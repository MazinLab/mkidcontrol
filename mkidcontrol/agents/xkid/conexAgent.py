"""
3 January 2023
Author: Noah Swimmer

Agent for controlling the CONEX-AG-M100D Piezo Motor Mirror Mount (https://www.newport.com/p/CONEX-AG-M100D) that serves
as a tip/tilt mirror for XKID aiding in both alignment and dithering.

Axis syntax is: U -> rotation around the y-axis, V -> rotation around the y-axis

TODO: Control software
"""

import logging
import sys
import time
import numpy as np
import threading

import serial

from mkidcontrol.mkidredis import RedisError
import mkidcontrol.mkidredis as redis
import mkidcontrol.util as util
from mkidcontrol.commands import COMMANDSCONEX, LakeShoreCommand
from mkidcontrol.devices import SerialDevice

logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger(__name__)

QUERY_INTERVAL = 1

STATUS_KEY = "status:device:conex:status"
SN_KEY = "status:device:conex:sn"
FIRMWARE_KEY = "status:device:conex:firmware"

TS_KEYS = ()

SETTING_KEYS = tuple(COMMANDSCONEX.keys())
COMMAND_KEYS = tuple([f"command:{key}" for key in list(SETTING_KEYS)])


class Conex(SerialDevice):
    CONTROLLER_STATES = {"14": "CONFIGURATION",
                         "28": "MOVING CL",
                         "29": "STEPPING OL",
                         "32": "READY from Reset",
                         "33": "READY from MOVING CL",
                         "34": "READY from DISABLE",
                         "35": "READY from JOGGING OL",
                         "36": "READY from STEPPING OL",
                         "3C": "DISABLE from READY OL",
                         "3D": "DISABLE from MOVING CL",
                         "46": "JOGGING OL"}

    def __init__(self, port, controller=1, timeout=1, connect=True, initializer=None):
        # TODO
        super().__init__(name="Conex", port=port, baudrate=921600, timeout=timeout, bytesize=serial.EIGHTBITS,
                         stopbits=serial.STOPBITS_ONE, xonxoff=True, terminator='\r\n')
        pass

        self.ctrlN = controller  # Controller can be an int between 1-31 inclusive or a string of 1 to 2 characters that
        # represents the possible number values (i.e. 1, "1", and "01" will all work)
        self.sn = None
        self.firmware = None

        self.u_lower_limit = -np.inf
        self.v_lower_limit = -np.inf
        self.u_upper_limit = np.inf
        self.v_upper_limit = np.inf

        self.initializer = initializer
        self._monitor_thread = None
        self._initialized = False
        self.initialized_at_last_connect = False

        if connect:
            self.connect(raise_errors=False)
            q = [float(self.query(q)) for q in ('SLU?', 'SLV?', 'SRU?', 'SRV?')]
            self.u_lower_limit = q[0]
            self.v_lower_limit = q[1]
            self.u_upper_limit = q[2]
            self.v_upper_limit = q[3]

    @property
    def id_number(self):
        return self.query("ID?")

    @property
    def firmware(self):
        return self.query("VE?")

    @property
    def limits(self):
        """
        Hardware limit for U, V in degrees
        returns dict with keys (umin, umax, vmin, vmax)
        """
        return dict(umin=self.u_lower_limit, vmin=self.v_lower_limit,
                    umax=self.u_upper_limit, vmax=self.v_upper_limit)

    def set_limit(self, cmd:str, limit:(str, float)):
        msg = cmd+str(limit)
        try:
            log.debug(f"Setting {cmd} with value {limit}")
            self.send(msg)
        except Exception as e:
            raise IOError(f"Failed to command conex: {e}")
        try:
            new_limit = float(self.query(f"{cmd}?"))
        except Exception as e:
            raise IOError(f"Failed to query new limit value: {e}")

        if cmd == "SLU":
            self.u_lower_limit = new_limit
        elif cmd == "SLV":
            self.v_lower_limit = new_limit
        elif cmd == "SRU":
            self.u_upper_limit = new_limit
        elif cmd == "SRV":
            self.v_upper_limit = new_limit
        else:
            raise ValueError(f"Invalid limit command sent! {cmd}")

    def status(self):
        """
        Check status of the conex

        :return: Tuple of (status code, status string, status message)
        :raises: IOError if there are communication issues
        """
        status_msg = self.query("TS?")
        err = status_msg[:4]
        status_code = status_msg[4:]

        if err == '0020':
            raise IOError("Motion time out")
        elif int(err, 16) > 0:
            raise IOError(f"Unknown Err - {err}")

        try:
            status = self.CONTROLLER_STATES[status_code]
        except (KeyError, ValueError) as e:
            raise ValueError(f"Invalid status code read by conex: {e}")

        return (status_code, status, status_msg)

    def ready(self):
        """
        Check status of the conex

        :return
        True if conex is ready for another command
        False is conex isn't ready
        :raises
        IOError if there are communication issues
        """
        # TODO: Just call self.status[0] to get the status_code?
        # status = self.status()
        # return int(status[0]) in (32, 33, 34, 35, 36)

        status_msg = self.query("TS?")
        err = status_msg[:4]
        status = status_msg[4:]

        if err == '0020':
            raise IOError("Motion time out")
        elif int(err, 16) > 0:
            raise IOError(f"Unknown Err - {err}")

        return int(status) in (32, 33, 34, 35, 36)

    def move(self, pos:(tuple, list, np.array), blocking=False, timeout=5.):
        """
        Move mirror to new position

        :param pos: [U,V] tuple/list/array position in degrees (Conex truncates these floats at 3 decimal places)
        :param blocking: If True, don't return until the move is complete
        :param timeout: error out if it takes too long for move to complete. Ignored if not blocking. Requires
         significant time even though the moves themselves are fast
        """
        with self._rlock:
            if not self.in_bounds(position=pos):
                raise ValueError('Target position outside of limits. Aborted move')
            self.send(f"PAU{pos[0]}")
            self.ser.flush()  # wait until the write command finishes sending
            self.send(f"PAV{pos[1]}")  # Conex can move both axes at once
            if blocking:
                self.ser.flush()
        if blocking:
            self.ser.flush()
            t = time.time()
            while not self.ready():
                if time.time() - t > timeout:
                    status = self.status()
                    raise IOError(f"Move timed out. Status: {status[1]} (code {status[0]})")
                time.sleep(0.001)

    def home(self, blocking=False):
        """
        Move the conex back to position (0, 0)
        """
        self.move((0, 0), blocking=blocking)

    def position(self):
        u_pos = self.query("TPU?")
        v_pos = self.query("TPV?")
        return (float(u_pos), float(v_pos))

    def in_bounds(self, position:(tuple, list, np.array)=None, u:float=None, v:float=None):
        """
        :param Either position in the format [u,v] or u AND v
        Position must be type <float> in degrees
        The position tuple (u,v) will supersede individual coordinates being passed
        :
        :return: True if position is within the positioning limits, False otherwise
        """
        if position is None:
            if (u is None) or (v is None):
                raise ValueError(f"Cannot determine position is in bounds without coordinates (either [u,v] or u and v)")
        else:
            u = position[0]
            v = position[1]

        inbounds = ((self.u_lower_limit <= u <= self.u_upper_limit) and
                    (self.v_lower_limit <= v <= self.v_upper_limit))
        log.info(f"({u}, {v}) in bounds status is {inbounds}")
        return inbounds

    def stop(self):
        """
        Stops a move in progress on the controller.
        """
        self.send("ST")

    def reset(self):
        """
        Issue a hardware reset of the controller, equivalent to a power-up.
        """
        self.send("RS")

    def disable(self):
        """
        Disables the controller. Checks to ensure it has been disabled successfully
        """
        self.send("MM0")
        disabled = self.status()[1].split(" ")[0] == "DISABLE"
        if disabled:
            log.info(f"Successfully disabled the conex controller")
        else:
            log.warning(f"Unable to disable the conex controller!")

    def enable(self):
        """
        Enables the controller. Checks to ensure it has been enabled successfully
        """
        self.send("MM1")
        enabled = self.status()[1].split(" ")[0] == "READY"
        if enabled:
            log.info(f"Successfully enabled the conex controller")
        else:
            log.warning(f"Unable to enable the conex controller!")

    def format_msg(self, msg:str):
        """
        Overrides method from base class
        Command syntax is 'nnAAxx\r\n'
        nn - Controller number (typically 1 unless stages are daisy chained together)
        AA - Command name
        xx - Optional or required value or "?" to query current value

        If final characters of msg to not match self.terminator ('\r\n'), add the terminator
        If initial character does not match controller number
        """
        if msg and msg[-2:] != self.terminator:
            msg = msg+self.terminator
        if msg and ((msg[:1] != str(self.ctrlN)) or (msg[:2] != str(self.ctrlN))):
            msg = str(self.ctrlN) + msg
        return msg.encode('utf-8')

    def query(self, cmd: str, **kwargs):
        """
        Overrides method from base class
        Send command and wait for a response, kwargs passed to send, raises only IOError
        Response syntax is nnAAxx...xx
        nn - Controller number
        AA - Command name
        xx...xx - Response (of length dependent on the command sent).

        Checks to ensure the command is received with the proper syntax, removes qualifiers, and returns the query response
        """
        with self._rlock:
            try:
                self.send(cmd, **kwargs)
                time.sleep(.1)
                received = self.receive()
                cmd = cmd.rstrip("?")
                if (received[:1] == str(self.ctrlN)) or (received[:2] == str(self.ctrlN)):
                    received = received.lstrip(str(self.ctrlN))
                else:
                    raise IOError(f"Received inaccurate message from Conex!")
                if (received[:2] == cmd) or (received[:3] == cmd):
                    received = received.lstrip(cmd)
                else:
                    raise IOError(f"Received inaccurate message from Conex!")
                return received
            except Exception as e:
                raise IOError(e)


class ConexManager:
    """
    The ConexManager manages the Conex() base class

    It also implements a thread safe dither routine

    Possible states:
    - "Unknown"
    - "Offline"
    - "Idle"
    - "Stopped"
    - "Moving"
    - "Dither"
    - "Error"
    """

    def __init__(self, port):
        self.conex = Conex(port=port)
        self._completed_dithers = []  # list of completed dithers
        self._movement_thread = None  # thread for moving/dithering
        self._halt_dither = True
        self._rlock = threading.RLock()
        self._startedMove = 0  # number of times start_move was called (not dither). Reset in queryMove and start_dither
        self._completedMoves = 0  # number of moves completed (not dither)

        self.state = ('Unknown', 'Unknown')
        try:
            if self.conex.ready():
                self._updateState('Idle')
        except:
            pass
        self.cur_status = self.status()

    def _updateState(self, newState):
        with self._rlock:
            self.state = (self.state[1], newState)

    def status(self):
        pos = (np.NaN, np.NaN)
        status = ''
        try:
            status = self.conex.status
            pos = self.conex.position()
            log.debug(f"Conex: {status[1]} @ pos {pos}")
        except (IOError, serial.SerialException):
            log.error('Unable to get conex status', exc_info=True)
            self._halt_dither = True
            self._updateState('Offline')
        return {'state': self.state, 'pos': pos, 'conexstatus': status, 'limits': self.conex.limits}

if __name__ == "__main__":

    redis.setup_redis()
    util.setup_logging('conexAgent')

    try:
        conex = Conex('/dev/conex')
        redis.store({SN_KEY: conex.id_number})
        redis.store({FIRMWARE_KEY: conex.firmware})
        redis.store({STATUS_KEY: "OK"})
    except RedisError as e:
        log.error(f"Redis server error! {e}")
        sys.exit(1)
    except Exception as e:
        log.critical(f"Could not connect to the conex! Error {e}")
        redis.store({STATUS_KEY: f"Error: {e}"})
        sys.exit(1)

