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

TS_KEYS = ()

SETTING_KEYS = tuple(COMMANDSCONEX.keys())
COMMAND_KEYS = tuple([f"command:{key}" for key in list(SETTING_KEYS)])


class Conex(SerialDevice):
    def __init__(self, port, controller=1, timeout=1, connect=True, initializer=None):
        # TODO
        super().__init__(name="Conex", port=port, baudrate=921600, timeout=timeout, bytesize=serial.EIGHTBITS,
                         stopbits=serial.STOPBITS_ONE, xonxoff=True, terminator='\r\n')
        pass

        self.ctrlN = controller  # Controller can be an int between 1-31 inclusive or a string of 1 to 2 characters that
        # represents the possible number values (i.e. 1, "1", and "01" will all work)
        self.sn = None
        self.firmware = None

        self.u_lowerLimit = -np.inf
        self.v_lowerLimit = -np.inf
        self.u_upperLimit = np.inf
        self.v_upperLimit = np.inf

        self.initializer = initializer
        self._monitor_thread = None
        self._initialized = False
        self.initialized_at_last_connect = False

        if connect:
            self.connect(raise_errors=False)

    @property
    def limits(self):
        """
        Hardware limit for U, V in degrees
        returns dict with keys (umin, umax, vmin, vmax)
        """

        q = [float(self.query(q)) for q in ('SLU?', 'SLV?', 'SRU?', 'SRV?')]
        self.u_lowerLimit = q[0]
        self.v_lowerLimit = q[1]
        self.u_upperLimit = q[2]
        self.v_upperLimit = q[3]

        return dict(umin=self.u_lowerLimit, vmin=self.v_lowerLimit,
                    umax=self.u_upperLimit, vmax=self.v_upperLimit)

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

    def status(self):
        status = self.query("TS")


if __name__ == "__main__":

    redis.setup_redis()
    util.setup_logging('conexAgent')

    try:
        conex = Conex('/dev/conex')
    except RedisError as e:
        log.error(f"Redis server error! {e}")
        sys.exit(1)
    except Exception as e:
        log.critical(f"Could not connect to the conex! Error {e}")
        redis.store({STATUS_KEY: f"Error: {e}"})
        sys.exit(1)

