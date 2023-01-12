"""
3 January 2023
Author: Noah Swimmer

Agent for controlling the CONEX-AG-M100D Piezo Motor Mirror Mount (https://www.newport.com/p/CONEX-AG-M100D) that serves
as a tip/tilt mirror for XKID aiding in both alignment and dithering.

Axis syntax is: U -> rotation around the y-axis, V -> rotation around the y-axis
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


class ConexController:
    """
    The ConexController manages the Conex() base class

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
            status = self.conex.status()
            pos = self.conex.position()
            log.debug(f"Conex: {status[1]} @ pos {pos}")
        except (IOError, serial.SerialException):
            log.error('Unable to get conex status', exc_info=True)
            self._halt_dither = True
            self._updateState('Offline')
        return {'state': self.state, 'pos': pos, 'conexstatus': status, 'limits': self.conex.limits}

    def queryMove(self):
        """
        Checks to see if move completed

        It should be thread safe. Even if you hit the move button several times really fast

        OUTPUTS:
            dictionary {'completed':True/False, 'status':self.cur_status}
        """
        if self._completedMoves > 0:  # don't lock if no moves completed. Reading is thread safe
            with self._rlock:  # if at least one move completed then lock
                if self._completedMoves > 0:  # need to check again for thread safety (maybe started two moves but only 1 completed)
                    self._completedMoves -= 1
                    self._startedMove -= 1
                    self._startedMove = max(0, self._startedMove)
                    return {'completed': True, 'status': self.cur_status}
        return {'completed': False, 'status': self.cur_status}

    def queryDither(self):
        """
        returns the dictionary containing information about an ongoing or completed dither

        keys:
            status - The current status of the conex manager
            estTime - ?? Not implemented right now
            dither - A dictionary containing the oldest completed dither that hasn't been popped yet
                     see dither() output
                     If no completed dithers then None
            completed - True or False
        """
        dith = None
        estTime = 0
        completed = False
        if len(self._completed_dithers) > 0:  # Reading is thread safe
            with self._rlock:  # only lock if at least one dither completed
                try:
                    dith = self._completed_dithers.pop(0)
                    completed = True
                except IndexError:
                    pass
        if dith is None:  # check if a dither was popped
            estTime = time.time() + 1  # estimated unix time of when dither will complete
        return {'status': self.cur_status, 'estTime': estTime, 'dither': dith, 'completed': completed}

    def stop(self, wait=False):
        """
        stops the current movement or dither

        if wait is False then it forcibly writes to the conex to tell it to stop motion

        after that it waits for the movement thread to finish
        """
        log.debug('stopping conex')

        if self._movement_thread is not None and self._movement_thread.is_alive():
            with self._rlock:
                self._halt_dither = True
                if not wait:
                    self.conex.stop()  # puts conex in ready state so that _movement thread will finish
                self._updateState('Stopped')
                self.cur_status = self.status()
            self._movement_thread.join()  # not in rlock
            with self._rlock:
                self.cur_status = self.status()  # could change in other thread
        else:
            with self._rlock:
                self.cur_status = self.status()
        return self.cur_status

    def start_dither(self, dither_dict):
        """
        Starts dither in a new thread
        """
        log.debug('starting dither')
        self.stop(wait=False)  # stop whatever we were doing before (including a previous dither)
        with self._rlock:
            self.cur_status = self.status()
            if self.cur_status['state'] == 'Offline': return False
            self._halt_dither = False
            self._startedMove = 0
        self._preDitherPos = self.cur_status['pos']
        self._movement_thread = threading.Thread(target=self.dither_two_point, args=(dither_dict,), name="Dithering thread")
        self._movement_thread.daemon = True
        self._movement_thread.start()
        return True

    def dither_two_point(self, dither_dict):
        """
        INPUTS:
            dither_dict - dictionary with keys:
                        startx: (float) start x loc in conex degrees
                        endx: (float) end x loc
                        starty: (float) start y loc
                        endy: (float) end y loc
                        n: (int) number of steps in square grid
                        t: (float) dwell time in seconds
                        subStep: (float) degrees to offset for subgrid pattern
                        subT: (float) dwell time for subgrid

                        subStep and subT are optional

        appends a dictionary to the self._completed_dithers attribute
            keys - same as dither_dict but additionally
                   it has keys (xlocs, ylocs, startTimes, endTimes)

        """
        points = dither_two_point_positions(dither_dict['startx'], dither_dict['starty'], dither_dict['stopx'],
                                            dither_dict['stopy'], dither_dict['n'])

        subDither = 'subStep' in dither_dict.keys() and dither_dict['subStep'] > 0 and \
                    'subT' in dither_dict.keys() and dither_dict['subT'] > 0

        x_locs = []
        y_locs = []
        startTimes = []
        endTimes = []
        for p in points:
            startTime, endTime = self._dither_move(p[0], p[1], dither_dict['t'])
            if startTime is not None:
                x_locs.append(self.cur_status['pos'][0])
                y_locs.append(self.cur_status['pos'][1])
                startTimes.append(startTime)
                endTimes.append(endTime)
            if self._halt_dither: break

            # do sub dither if neccessary
            if subDither:
                x_sub = [-dither_dict['subStep'], 0, dither_dict['subStep'], 0]
                y_sub = [0, dither_dict['subStep'], 0, -dither_dict['subStep']]
                for i in range(len(x_sub)):
                    if self.conex.in_bounds((p[0] + x_sub[i], p[1] + y_sub[i])):
                        startTime, endTime = self._dither_move(p[0] + x_sub[i], p[1] + y_sub[i], dither_dict['subT'])
                        if startTime is not None:
                            x_locs.append(self.cur_status['pos'][0])
                            y_locs.append(self.cur_status['pos'][1])
                            startTimes.append(startTime)
                            endTimes.append(endTime)
                    if self._halt_dither: break
            if self._halt_dither: break

        # Dither has completed (or was stopped prematurely)
        if not self._halt_dither:  # no errors and not stopped
            self.move(*self._preDitherPos)
            with self._rlock:
                if not self._halt_dither:  # still no errors nor stopped
                    self._updateState("Idle")
                self.cur_status = self.status()

        dith = dither_dict.copy()
        dith['xlocs'] = x_locs  # could be empty if errored out or stopped too soon
        dith['ylocs'] = y_locs
        dith['startTimes'] = startTimes
        dith['endTimes'] = endTimes
        with self._rlock:
            self._completed_dithers.append(dith)

    def dither(self, dither_dict):
        """
        INPUTS:
            dither_dict - dictionary with keys:
                        startx: (float) start x loc in conex degrees
                        endx: (float) end x loc
                        starty: (float) start y loc
                        endy: (float) end y loc
                        n: (int) number of steps in square grid
                        t: (float) dwell time in seconds
                        subStep: (float) degrees to offset for subgrid pattern
                        subT: (float) dwell time for subgrid

                        subStep and subT are optional

        appends a dictionary to the self._completed_dithers attribute
            keys - same as dither_dict but additionally
                   it has keys (xlocs, ylocs, startTimes, endTimes)

        """
        # Ennabling single direction sweeps
        if dither_dict['startx'] == dither_dict['endx']:
            x_list = np.linspace(dither_dict['startx'], dither_dict['endx'], 1)
        else:
            x_list = np.linspace(dither_dict['startx'], dither_dict['endx'], dither_dict['n'])
        if dither_dict['starty'] == dither_dict['endy']:
            y_list = np.linspace(dither_dict['starty'], dither_dict['endy'], 1)
        else:
            y_list = np.linspace(dither_dict['starty'], dither_dict['endy'], dither_dict['n'])

        subDither = 'subStep' in dither_dict.keys() and dither_dict['subStep'] > 0 and \
                    'subT' in dither_dict.keys() and dither_dict['subT'] > 0

        x_locs = []
        y_locs = []
        startTimes = []
        endTimes = []
        for x in x_list:
            for y in y_list:
                startTime, endTime = self._dither_move(x, y, dither_dict['t'])
                if startTime is not None:
                    x_locs.append(self.cur_status['pos'][0])
                    y_locs.append(self.cur_status['pos'][1])
                    startTimes.append(startTime)
                    endTimes.append(endTime)
                if self._halt_dither: break

                # do sub dither if neccessary
                if subDither:
                    x_sub = [-dither_dict['subStep'], 0, dither_dict['subStep'], 0]
                    y_sub = [0, dither_dict['subStep'], 0, -dither_dict['subStep']]
                    for i in range(len(x_sub)):
                        if self.conex.in_bounds((x + x_sub[i], y + y_sub[i])):
                            startTime, endTime = self._dither_move(x + x_sub[i], y + y_sub[i], dither_dict['subT'])
                            if startTime is not None:
                                x_locs.append(self.cur_status['pos'][0])
                                y_locs.append(self.cur_status['pos'][1])
                                startTimes.append(startTime)
                                endTimes.append(endTime)
                        if self._halt_dither: break
                if self._halt_dither: break
            if self._halt_dither: break

        # Dither has completed (or was stopped prematurely)
        if not self._halt_dither:  # no errors and not stopped
            self.move(*self._preDitherPos)
            with self._rlock:
                if not self._halt_dither:  # still no errors nor stopped
                    self._updateState("Idle")
                self.cur_status = self.status()

        dith = dither_dict.copy()
        dith['xlocs'] = x_locs  # could be empty if errored out or stopped too soon
        dith['ylocs'] = y_locs
        dith['startTimes'] = startTimes
        dith['endTimes'] = endTimes
        with self._rlock:
            self._completed_dithers.append(dith)

    def _dither_move(self, x, y, t):
        """
            Helper function for dither()

            The state after this function call will be one of:
                "error: ..." - If there there was an error during the move
                "processing" - If everything worked
        """
        polltime = 0.1  # wait for dwell time but have to check if stop was pressed periodically
        self.move(x, y)
        if self._halt_dither: return None, None  # Stopped or error during move
        self._updateState("Dither dwell for {:.1f} seconds".format(t))
        # dwell at position
        startTime = time.time()
        dwell_until = startTime + t
        endTime = time.time()
        with self._rlock:
            self.cur_status = self.status()
        while self._halt_dither == False and endTime < dwell_until:
            sleep = min(polltime, dwell_until - endTime)
            time.sleep(max(sleep, 0))
            endTime = time.time()
        return startTime, endTime

    def start_move(self, x, y):
        """
        Starts move in new thread
        """
        self.stop(wait=False)  # If the user wants to move, then forcibly stop whatever we were doing before (indcluding dithers)
        with self._rlock:
            self.cur_status = self.status()
            if self.cur_status['state'] == 'Offline': return False
            self._startedMove += 1
        self._movement_thread = threading.Thread(target=self.move, args=(x, y,),
                                       name=f'Move to ({x}, {y})')
        self._movement_thread.daemon = True
        self._movement_thread.start()

        return True

    def move(self, x, y):
        """
        Tells conex to move and collects errors
        """
        self._updateState(f'Moving to {x:.2f}, {y:.2f}')
        try:
            self.conex.move((x, y), blocking=True)  # block until conex is done moving (or stopped)
            if self._startedMove > 0:
                self._updateState('Idle')
            log.debug(f'moved to ({x}, {y})')
        except (IOError, serial.SerialException) as e:  # on timeout it raise IOError
            self._updateState(f'Error: move to {x:.2f}, {y:.2f} failed')
            self._halt_dither = True
            log.error('Error on move', exc_info=True)
        except:  # I dont think this should happen??
            self._updateState(f'Error: move to {x:.2f}, {y:.2f} failed')
            self._halt_dither = True
            log.error('Unexpected error on move', exc_info=True)
        if self._startedMove > 0:
            with self._rlock:
                self.cur_status = self.status()
                self._completedMoves += 1


def dither_two_point_positions(start_x, start_y, stop_x, stop_y, user_n_steps, single_pixel_move=0.015):
    if user_n_steps == 1:
        log.error('Number of steps must be greater than one!')
        return
    if stop_y == start_y and stop_x == start_x:
        log.error('No movement specified in x or y')
        return

    n_steps = user_n_steps - 1
    points = []
    interval_x = ((stop_x - (0.5 * single_pixel_move)) - start_x) / (n_steps / 2)
    interval_y = ((stop_y - (0.5 * single_pixel_move)) - start_y) / (n_steps / 2)

    x_list = np.arange(start_x, stop_x, interval_x)
    y_list = np.arange(start_y, stop_y, interval_y)
    if start_x == stop_x:
        x_list = np.zeros(n_steps)
    if start_y == stop_y:
        y_list = np.zeros(n_steps)

    offset_x = np.round(x_list + (interval_x / 2 + 0.5 * single_pixel_move), 3)
    offset_y = np.round(y_list + (interval_y / 2 + 0.5 * single_pixel_move), 3)
    x_grid = np.round(np.sort(np.concatenate((x_list, offset_x[offset_x <= round(stop_x, 4)]))), 3)
    y_grid = np.round(np.sort(np.concatenate((y_list, offset_y[offset_y <= round(stop_y, 4)]))), 3)
    cycle = 1

    if not (np.all(x_grid == 0) or np.all(y_grid == 0)):
        points.append((start_x, start_y))

    while cycle <= n_steps:
        for i in range(cycle + 1):
            rev = cycle - i
            points.append((x_grid[i], y_grid[rev]))
        cycle += 1
    cycle = 1
    second_points = []
    while cycle <= (n_steps - 1):
        for i in range(cycle + 1):
            rev = cycle - i
            second_points.insert(0, (x_grid[-1 - i], y_grid[-1 - rev]))
        cycle += 1
    points = points + second_points
    if not (np.all(x_grid == 0) or np.all(y_grid == 0)):
        points.append((x_grid[-1], y_grid[-1]))
    return points


if __name__ == "__main__":

    redis.setup_redis()
    util.setup_logging('conexAgent')

    try:
        cc = ConexController(port='/dev/conex')
        redis.store({SN_KEY: cc.conex.id_number})
        redis.store({FIRMWARE_KEY: cc.conex.firmware})
        redis.store({STATUS_KEY: "OK"})
    except RedisError as e:
        log.error(f"Redis server error! {e}")
        sys.exit(1)
    except Exception as e:
        log.critical(f"Could not connect to the conex! Error {e}")
        redis.store({STATUS_KEY: f"Error: {e}"})
        sys.exit(1)

    try:
        while True:
            for key, val in redis.listen(COMMAND_KEYS):
                log.debug(f"conexAgent received {key}: {val}.")
                key = key.removeprefix("command:")
                if key in SETTING_KEYS:
                    try:
                        cmd = LakeShoreCommand(key, val)
                    except ValueError as e:
                        log.warning(f"Ignoring invalid command ('{key}={val}'): {e}")
                        continue
                    try:
                        log.info(f"Processing command {cmd}")
                        if key == FILTERWHEEL_POSITION_KEY:
                            fw.set_filter_pos(cmd.command_value)
                            redis.store({cmd.setting: cmd.value})
                            redis.store({FILTERWHEEL_FILTER_KEY: FILTERS[cmd.value]})
                            redis.store({STATUS_KEY: "OK"})
                    except IOError as e:
                        redis.store({STATUS_KEY: f"Error {e}"})
                        log.error(f"Comm error: {e}")
    except RedisError as e:
        log.error(f"Redis server error! {e}")
        sys.exit(1)

