"""
Author: Noah Swimmer
29 March 2022

Controls the Heat Switch Motor from Zaber using their library which essentially is a wrapper for pyserial.

The units used are always in the 'Zaber Native Units'. Essentially what this means is that (for this device,
model: T-NM17, SN: 4294967295) the motor runs from 0 to 8388606. The middle point is 4194303. The phase of the stepper
motor is controlled by the least significant byte of the position, meaning that the device may initialize +/-2 full
steps upon restart (essentially reporting that it is in a position that it is not in).
So, we must ensure that when initializing the motor this is taken into account so we don't try to open too much and hit
a limit of the motor and damage it or we don't try to close it too much and do damage by trying to clamp on the heat
switch too tightly.

Note: Documentation for zaber python library exists at https://www.zaber.com/software/docs/motion-library/
Command syntax exists at (lower level comms): https://www.zaber.com/documents/ZaberT-SeriesProductsUsersManual2.xx.pdf
"""

import sys
import time
import logging
import threading
import serial

from mkidcontrol.mkidredis import MKIDRedis, RedisError
import mkidcontrol.util as util
from mkidcontrol.devices import HeatswitchPosition, write_persisted_state, load_persisted_state
from mkidcontrol.commands import COMMANDSHS, SimCommand
from zaber_motion import Library
from zaber_motion.binary import Connection, BinarySettings, CommandCode

QUERY_INTERVAL = 1
TIMEOUT = 4194303 * 1.25 / 0.5e3  # Default timeout value is the number of steps + 25% divided by half the slowest speed we run at

log = logging.getLogger(__name__)

SETTING_KEYS = tuple(COMMANDSHS.keys())

DEFAULT_MAX_VELOCITY = 1e3  # Maximum velocity empirically found with ARCONS
DEFAULT_RUNNING_CURRENT = 13  # Current can be set between 10 (highest) and 127 (lowest). Lower current (higher number)
# will avoid damaging the heat switch if limit is reached by mistake
DEFAULT_ACCELERATION = 2  # Default acceleration from ARCONS
FULL_OPEN_POSITION = 0  # Hard limit of the motor opening
FULL_CLOSE_POSITION = 4194303  # Halfway point for motor position, physical hard stop with clamps closed on heat sinks

STATUS_KEY = 'status:device:heatswitch:status'  # OK | ERROR | OFF
HEATSWITCH_POSITION_KEY = "status:device:heatswitch:position"  # opened | opening | closed | closing
MOTOR_POS = "status:device:heatswitch:motor:position"  # Integer between 0 and 4194303

HEATSWITCH_MOVE_KEY = "device-settings:heatswitch:position"
VELOCITY_KEY = "device-settings:heatswitch:max-velocity"
RUNNING_CURRENT_KEY = "device-settings:heatswitch:running-current"
ACCELERATION_KEY = "device-settings:heatswitch:acceleration"

COMMAND_KEYS = [f"command:{k}" for k in SETTING_KEYS]
TS_KEYS = (MOTOR_POS,)


def close():
    redis.publish(f"command:{HEATSWITCH_MOVE_KEY}", HeatswitchPosition.CLOSE, store=False)


def open():
    redis.publish(f"command:{HEATSWITCH_MOVE_KEY}", HeatswitchPosition.OPEN, store=False)


def is_opened():
    return not (redis.read(HEATSWITCH_POSITION_KEY) == HeatswitchPosition.CLOSED)


def is_closed():
    return redis.read(HEATSWITCH_POSITION_KEY) == HeatswitchPosition.CLOSED


def monitor_callback(mpos, mstate):
    timeseries_d = {MOTOR_POS: mpos}
    d = {HEATSWITCH_POSITION_KEY: mstate}
    try:
        if mpos is None:
            # N.B. If there is an error on the query, the value passed is None
            redis.store({STATUS_KEY: "Error"})
        else:
            redis.store(timeseries_d, timeseries=True)
            redis.store(d)
            redis.store({STATUS_KEY: "OK"})
    except RedisError:
        log.warning('Storing motor position to redis failed')


def compute_initial_state(heatswitch):
    """
    Initial states can be opening, closing, opened, or closed
    """
    try:
        if heatswitch.initialized:
            pass
        else:
            heatswitch._initialize_position()

        # NB: This is handled by the call to _initialize_position() which keeps the motor and DB in sync
        position = redis.read(MOTOR_POS)[1]

        if position == FULL_CLOSE_POSITION:
            initial_state = "closed"
        elif position == FULL_OPEN_POSITION:
            initial_state = "opened"
        else:
            if redis.read(HEATSWITCH_POSITION_KEY) == HeatswitchPosition.OPENING:
                initial_state = "opening"
            else:
                initial_state = "closing"
    except IOError:
        log.critical('Lost heatswitch connection during agent startup. defaulting to unknown')
        initial_state = "unknown"
    except RedisError:
        log.critical('Lost redis connection during compute_initial_state startup.')
        raise
    log.info(f"\n\n------ Initial State is: {initial_state} ------\n")
    return initial_state


class HeatswitchMotor:
    def __init__(self, port, set_mode=True):
        c = Connection.open_serial_port(port)
        self.hs = c.detect_devices()[0]

        self.initialized = False
        self.last_recorded_position = None
        self.last_move = 0

        # Initializes the heatswitch to
        self._initialize_position()

        if set_mode:
            self.update_binary_setting(BinarySettings.DEVICE_MODE, 8)
            self.update_binary_setting(BinarySettings.TARGET_SPEED, DEFAULT_MAX_VELOCITY)
            self.update_binary_setting(BinarySettings.RUNNING_CURRENT, DEFAULT_RUNNING_CURRENT)
            self.update_binary_setting(BinarySettings.ACCELERATION, DEFAULT_ACCELERATION)

        self.running_current = self.hs.settings.get(BinarySettings.RUNNING_CURRENT)
        self.acceleration = self.hs.settings.get(BinarySettings.ACCELERATION)
        self.max_position = min(self.hs.settings.get(BinarySettings.MAXIMUM_POSITION), FULL_CLOSE_POSITION)
        self.min_position = FULL_OPEN_POSITION
        self.max_velocity = self.hs.settings.get(BinarySettings.TARGET_SPEED)
        self.max_relative_move = self.hs.settings.get(BinarySettings.MAXIMUM_RELATIVE_MOVE)
        self.device_mode = self.hs.settings.get(BinarySettings.DEVICE_MODE)

    def _initialize_position(self):
        """
        :return:
        """
        reported_position = int(self.motor_position())
        last_recorded_position = int(redis.read(MOTOR_POS)[1])

        distance = abs(reported_position - last_recorded_position)

        log.info(f"The last position recorded to redis was {last_recorded_position}. "
                 f"The device thinks it is at a position of {reported_position}, a difference of {distance} steps")

        if distance == 0:
            log.info(f"Device is in the same state as during the previous connection. Motor is in position {last_recorded_position}.")
            # self.hs.generic_command(CommandCode.SET_CURRENT_POSITION, last_recorded_position)
        else:
            log.warning(f"Device was last recorded in position {last_recorded_position}, now thinks that it is at "
                        f"{reported_position}. Setting the position to {last_recorded_position}. If unrecorded movement"
                        f" was made, YOU MUST SET THE CURRENT POSITION MANUALLY")
            self.hs.generic_command(CommandCode.SET_CURRENT_POSITION, last_recorded_position)

        self.initialized = True
        self.last_recorded_position = self.motor_position()

    def state(self):
        if self.motor_position() == FULL_CLOSE_POSITION:
            log.debug(f"Motor is {HeatswitchPosition.CLOSED}")
            return HeatswitchPosition.CLOSED
        elif self.motor_position() == FULL_OPEN_POSITION:
            log.debug(f"Motor is {HeatswitchPosition.OPENED}")
            return HeatswitchPosition.OPENED
        else:
            if self.last_move >= 0:
                log.debug(f"Motor is {HeatswitchPosition.CLOSING}")
                return HeatswitchPosition.CLOSING
            else:
                log.debug(f"Motor is {HeatswitchPosition.OPENING}")
                return HeatswitchPosition.OPENING

    def motor_position(self):
        for i in range(5):
            try:
                position = self.hs.get_position()
                log.debug(f"Motor has reported that it is at position {position}")
                return position
            except Exception as e:
                log.debug(f"Error in querying heat switch motor. Attempt {i+1} of 5 failed. Trying again.")

    def move_to(self, pos, timeout=TIMEOUT, error_on_disallowed=False):
        """
        TODO: Test and validate
        :param pos:
        :param error_on_disallowed:
        :return:
        """
        last_pos = self.last_recorded_position
        if (last_pos < self.min_position) or (last_pos > self.max_position):
            if last_pos < self.min_position:
                log.warning(f"Requested move to {last_pos} not allowed. Attempting to restrict to FULL_OPEN_POSITION: {self.min_position}")
            elif last_pos > self.max_position:
                log.warning(f"Requested move to {last_pos} not allowed. Attempting to restrict to FULL_CLOSE_POSITION: {self.max_position}")

        if error_on_disallowed:
            raise Exception(f"Move requested from {last_pos} to {pos} not allowed. Out of range")
        else:
            if (last_pos < self.min_position) or (last_pos > self.max_position):
                if last_pos < self.min_position:
                    log.warning(f"Restricting move to FULL_OPEN_POSITION: {self.min_position}. Cannot move to {pos}")
                    pos = self.min_position
                elif last_pos > self.max_position:
                    log.warning(f"Restricting move to FULL_CLOSE_POSITION: {self.max_position}. Cannot move to {pos}")
                    pos = self.max_position
                try:
                    log.info(f"Move requested to {pos} from {last_pos}")
                    self.hs.move_absolute(pos, timeout=timeout)
                    self.last_move = pos - last_pos
                    self.last_recorded_position = pos
                    log.info(f"Successfully moved to {self.last_recorded_position}")
                except:
                    log.error(f"Move failed!!")
            else:
                try:
                    log.info(f"Move requested to {pos} from {last_pos}")
                    self.hs.move_absolute(pos, timeout=timeout)
                    self.last_move = pos - last_pos
                    self.last_recorded_position = pos
                    log.info(f"Successfully moved to {self.last_recorded_position}")
                except:
                    log.error(f"Move failed!!")

        return self.last_recorded_position

    def move_by(self, dist, timeout=TIMEOUT, error_on_disallowed=False):
        """
        TODO
        :param dist:
        :param error_on_disallowed:
        :return:
        """
        pos = self.last_recorded_position
        if abs(dist) > self.max_relative_move:
            if dist > 0:
                log.warning(f"Requested move of {dist} steps not allowed, restricting to the max value of {self.max_relative_move} steps")
                dist = self.max_relative_move
            elif dist < 0:
                log.warning(f"Requested move of {dist} steps not allowed, restricting to the max value of -{self.max_relative_move} steps")
                dist = -1 * self.max_relative_move

        final_pos = pos + dist
        new_final_pos = min(self.max_position, max(self.min_position, final_pos))

        if new_final_pos != final_pos:
            new_dist = new_final_pos - pos
            if error_on_disallowed:
                raise Exception(f"Move requested from {pos} to {final_pos} ({dist} steps) is not allowed")
            else:
                log.warning(f"Move requested from {pos} to {final_pos} ({dist} steps) is not "
                         f"allowed, restricting move to furthest allowed position of {new_final_pos} ({new_dist} steps).")
                try:
                    new_pos = self.hs.move_relative(new_dist, timeout=timeout)
                    if new_pos == self.motor_position():
                        self.last_recorded_position = new_pos
                        self.last_move = new_dist
                        log.info(f"Successfully moved to {self.last_recorded_position}")
                    else:
                        log.critical(f"Reported motor position ({self.motor_position()}) not equal to expected destination ({new_pos})!\n"
                                     f"Setting last recorded position to {self.motor_position()}")
                        self.last_recorded_position = self.motor_position()
                        self.last_move = self.motor_position() - pos
                except:
                    log.error(f"Move failed!!")
        else:
            log.info(f"Move requested from {pos} to {final_pos} ({dist} steps). Moving now...")
            try:
                new_pos = self.hs.move_relative(dist, timeout=timeout)
                if new_pos == self.motor_position():
                    self.last_recorded_position = new_pos
                    self.last_move = dist
                    log.info(f"Successfully moved to {self.last_recorded_position}")
                else:
                    log.critical(
                        f"Reported motor position ({self.motor_position()}) not equal to expected destination ({new_pos})!\n"
                        f"Setting last recorded position to {self.motor_position()}")
                    self.last_recorded_position = self.motor_position()
                    self.last_move = self.motor_position() - pos
            except:
                log.error(f"Move failed!!")

        return self.last_recorded_position

    def open(self):
        try:
            log.info(f"Opening heatswitch")
            self.move_by(-FULL_CLOSE_POSITION)
            log.info(f"Heatswitch now opened")
        except (IOError, serial.SerialException) as e:
            log.error("Could not open heatswitch!")
            raise Exception(f"Could not communicate with device: {e}")
        except Exception as e:
            log.error("Could not open heatswitch!")
            raise Exception(f"Move failed or illegal move requested: {e}")

    def close(self):
        try:
            log.info(f"Closing heatswitch")
            self.move_by(FULL_CLOSE_POSITION)
            log.info(f"Heatswitch now closed")
        except (IOError, serial.SerialException) as e:
            log.error("Could not close the heatswitch!")
            raise Exception(f"Could not communicate with device: {e}")
        except Exception as e:
            log.error("Could not close the heatswitch!")
            raise Exception(f"Move failed or illegal move requested: {e}")

    def update_binary_setting(self, key:(str, BinarySettings), value):
        if isinstance(key, str):
            key = key.split(':')[-1]
            KEYDICT = {'max-velocity': BinarySettings.TARGET_SPEED,
                       'running-current': BinarySettings.RUNNING_CURRENT,
                       'acceleration': BinarySettings.ACCELERATION}
            self.hs.settings.set(KEYDICT[key], value)
        else:
            self.hs.settings.set(key, value)

    def _set_position_value(self, value):
        """
        Tells this heat switch that it is at a different position than it thinks it is
        E.G. If the heatswitch reports that it is at 0 and one uses this function with <value>=10, it will then report
        that it is at position 10 without ever having moved.
        THIS IS SOLELY AN ENGINEERING FUNCTION AND SHOULD ONLY BE USED WITH EXTREME CARE
        """
        self.hs.generic_command(CommandCode.SET_CURRENT_POSITION, value)
        self.move_by(0)

    def monitor(self, interval: float, monitor_func: (callable, tuple), value_callback: (callable, tuple) = None):
        """
        Given a monitoring function (or is of the same) and either one or the same number of optional callback
        functions call the monitors every interval. If one callback it will get all the values in the order of the
        monitor funcs, if a list of the same number as of monitorables each will get a single value.

        Monitor functions may not return None.

        When there is a 1-1 correspondence the callback is not called in the event of a monitoring error.
        If a single callback is present for multiple monitor functions values that had errors will be sent as None.
        Function must accept as many arguments as monitor functions.
        """
        if not isinstance(monitor_func, (list, tuple)):
            monitor_func = (monitor_func,)
        if value_callback is not None and not isinstance(value_callback, (list, tuple)):
            value_callback = (value_callback,)
        if not (value_callback is None or len(monitor_func) == len(value_callback) or len(value_callback) == 1):
            raise ValueError('When specified, the number of callbacks must be one or the number of monitor functions')

        def f():
            while True:
                vals = []
                for func in monitor_func:
                    try:
                        vals.append(func())
                    except IOError as e:
                        log.error(f"Failed to poll {func}: {e}")
                        vals.append(None)

                if value_callback is not None:
                    if len(value_callback) > 1 or len(monitor_func) == 1:
                        for v, cb in zip(vals, value_callback):
                            try:
                                cb(v)
                            except Exception as e:
                                log.error(f"Callback {cb} error. arg={v}.", exc_info=True)
                    else:
                        cb = value_callback[0]
                        try:
                            cb(*vals)
                        except Exception as e:
                            log.error(f"Callback {cb} error. args={vals}.", exc_info=True)

                time.sleep(interval)

        self._monitor_thread = threading.Thread(target=f, name='Monitor Thread')
        self._monitor_thread.daemon = True
        self._monitor_thread.start()


if __name__ == "__main__":

    redis = MKIDRedis(ts_keys=TS_KEYS)
    util.setup_logging('heatswitchAgent')

    try:
        hs = HeatswitchMotor('/dev/heatswitch')
        redis.store({STATUS_KEY: "OK"})
    except RedisError as e:
        log.error(f"Redis server error! {e}")
        sys.exit(1)
    except Exception as e:
        log.critical(f"Could not connect to the heatswitch! Error {e}")
        redis.store({STATUS_KEY: f"Error: {e}"})
        sys.exit(1)

    hs.monitor(QUERY_INTERVAL, (hs.motor_position, hs.state), value_callback=monitor_callback)

    # controller = HeatswitchController(heatswitch=hs)

    try:
        while True:
            for key, val in redis.listen(COMMAND_KEYS):
                log.debug(f"HeatswitchAgent received {key}, {val}.")
                key = key.removeprefix('command:')
                if key in SETTING_KEYS:
                    try:
                        cmd = SimCommand(key, val)
                    except ValueError as e:
                        log.warning(f"Ignoring invalid command ('{key}={val}'): {e}")
                        continue
                    try:
                        log.info(f"Processing command '{cmd}'")
                        if key == HEATSWITCH_MOVE_KEY:
                            if val.lower() == "open":
                                hs.open()
                            elif val.lower() == "close":
                                hs.close()
                            else:
                                log.warning("Illegal command that was not previously handled!")
                        elif key in [VELOCITY_KEY, RUNNING_CURRENT_KEY, ACCELERATION_KEY]:
                            hs.update_binary_setting(key, val)
                        else:
                            log.warning(f"Unknown command! Ignoring")
                        redis.store({cmd.setting: cmd.value})
                        redis.store({STATUS_KEY: "OK"})
                    except IOError as e:
                        redis.store({STATUS_KEY: f"Error {e}"})
                        log.error(f"Comm error: {e}")
    except RedisError as e:
        log.error(f"Redis server error! {e}")
        sys.exit(1)