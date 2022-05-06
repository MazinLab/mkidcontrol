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

TODO: Udev rule for motor control

TODO: Error handling
"""

import sys
from collections import defaultdict
import time
import logging
import threading
import numpy as np
from transitions import MachineError, State
from transitions.extensions import LockedMachine

from mkidcontrol.mkidredis import MKIDRedis, RedisError
import mkidcontrol.util as util
from zaber_motion import Library
from zaber_motion.binary import Connection, BinarySettings, CommandCode

QUERY_INTERVAL = 1

log = logging.getLogger(__name__)

DEFAULT_MAX_VELOCITY = 3e3
DEFAULT_RUNNING_CURRENT = 13
DEFAULT_ACCELERATION = 2

FULL_OPEN_POSITION = 0
FULL_CLOSE_POSITION = 4194303

HS_POS = "status:device:heatswitch:position"  # OPENED | OPENING | CLOSED | CLOSING
MOTOR_POS = "status:device:heatswitch-motor:position"  # Integer between 0 and 4194303
HEATSWITCH_MOVE_KEY = f"command:{HS_POS}"

COMMAND_KEYS = (HEATSWITCH_MOVE_KEY,)
TS_KEYS = (MOTOR_POS,)


def write_persisted_state(statefile, state):
    try:
        with open(statefile, 'w') as f:
            f.write(f'{time.time()}:{state}')
    except IOError:
        # log.warning('Unable to log state entry', exc_info=True)
        pass


def monitor_callback(mpos):
    d = {k: v for k, v in [[MOTOR_POS, mpos]] if mpos is not None}
    try:
        redis.store(d, timeseries=True)
    except RedisError:
        log.warning('Storing motor position to redis failed')


class HeatswitchMotor:
    def __init__(self, port, timeout=(4194303 * 1.25)/3e3, set_mode=False):
        c = Connection.open_serial_port(port)
        self.hs = c.detect_devices()[0]

        self.last_recorded_position = None

        # Initializes the heatswitch to
        self._initialize_position()

        self.running_current = self.hs.settings.get(BinarySettings.RUNNING_CURRENT)
        self.acceleration = self.hs.settings.get(BinarySettings.ACCELERATION)
        self.max_position = min(self.hs.settings.get(BinarySettings.MAXIMUM_POSITION), FULL_CLOSE_POSITION)
        self.min_position = FULL_OPEN_POSITION
        self.max_velocity = self.hs.settings.get(BinarySettings.TARGET_SPEED)
        self.max_relative_move = self.hs.settings.get(BinarySettings.MAXIMUM_RELATIVE_MOVE)
        self.device_mode = self.hs.settings.get(BinarySettings.DEVICE_MODE)

        if set_mode:
            self.hs.settings.set(BinarySettings.DEVICE_MODE, 8)

    def _initialize_position(self):
        """
        TODO
        :return:
        """
        reported_position = int(self.hs.get_position())
        last_recorded_position = int(redis.read(MOTOR_POS)[1])  # TODO: Parse redis value

        distance = abs(reported_position - last_recorded_position)

        log.info(f"The last position recorded to redis was {last_recorded_position}. "
                 f"The device thinks it is at a position of {reported_position}, a difference of {distance} steps")

        if distance == 0:
            log.info(f"Device is in the same state as during the previous connection. Motor is in position {last_recorded_position}.")
            self.hs.generic_command(CommandCode.SET_CURRENT_POSITION, last_recorded_position)
        else:
            log.warning(f"Device was last recorded in position {last_recorded_position}, now thinks that it is at "
                        f"{reported_position}. Setting the position to {last_recorded_position}. If unrecorded movement"
                        f" was made, YOU MUST SET THE CURRENT POSITION MANUALLY")
            self.hs.generic_command(CommandCode.SET_CURRENT_POSITION, last_recorded_position)

        self.last_recorded_position = self.hs.get_position()

    def motor_position(self):
        return self.last_recorded_position

    def move_by(self, dist, error_on_disallowed=False):
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
                    self.last_recorded_position = self.hs.move_relative(new_dist)
                    log.info(f"Successfully moved to {self.last_recorded_position}")
                except:
                    log.error(f"Move failed!!")
        else:
            log.info(f"Move requested from {pos} to {final_pos} ({dist} steps). Moving now...")
            try:
                self.last_recorded_position = self.hs.move_relative(dist)
                log.info(f"Successfully moved to {self.last_recorded_position}")
            except:
                log.error(f"Move failed!!")

        return self.last_recorded_position

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
                            if v is not None:
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


class SimHeatswitch:
    def __init__(self):
        self.last_recorded_position = None

        self.running_current = 13
        self.acceleration = 2
        self.max_position = FULL_CLOSE_POSITION
        self.min_position = FULL_OPEN_POSITION
        self.max_velocity = 3e3
        self.max_relative_move = 4194303
        self.device_mode = 8

        self._initialize_position()

    def _initialize_position(self):
        self.last_recorded_position = min(np.random.randint(0, self.max_position * 8), self.max_position)
        log.info(f"Motor position initialized to {self.last_recorded_position}")

    def motor_position(self):
        return self.last_recorded_position

    def move_by(self, dist, error_on_disallowed=False):
        """
        TODO
        :param dist:
        :param error_on_disallowed:
        :return:
        """
        pos = self.last_recorded_position
        if abs(dist) > self.max_relative_move:
            if dist > 0:
                log.warning(
                    f"Requested move of {dist} steps not allowed, restricting to the max value of {self.max_relative_move} steps")
                dist = self.max_relative_move
            elif dist < 0:
                log.warning(
                    f"Requested move of {dist} steps not allowed, restricting to the max value of -{self.max_relative_move} steps")
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
                pos += new_dist
                self.last_recorded_position = pos
        else:
            log.info(f"Move requested from {pos} to {final_pos} ({dist} steps). Moving now...")
            pos += dist
            self.last_recorded_position = pos

        return self.last_recorded_position

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
                            if v is not None:
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


class HeatswitchController(LockedMachine):
    LOOP_INTERVAL = 1
    BLOCKS = defaultdict(set)

    def __init__(self):
        transitions = [
            # NOTE: Heatswitch will not allow the user to start reopening while closing or start reclosing while opening
            {'trigger': 'open', 'source': 'closed', 'dest': 'opening'},
            {'trigger': 'open', 'source': 'opening', 'dest': None, 'unless': 'hs_is_opened', 'after': 'open_heatswitch'},
            {'trigger': 'open', 'source': 'opened', 'dest': None, 'conditions': 'hs_is_opened'},

            {'trigger': 'next', 'source': 'closed', 'dest': None},
            {'trigger': 'next', 'source': 'opened', 'dest': None},

            {'trigger': 'next', 'source': 'opening', 'dest': 'opened', 'conditions': 'hs_is_opened'},
            {'trigger': 'next', 'source': 'opening', 'dest': None, 'unless': 'hs_is_opened', 'after': 'open_heatswitch'},

            {'trigger': 'close', 'source': 'opened', 'dest': 'closing'},
            {'trigger': 'close', 'source': 'closing', 'dest': None, 'unless': 'hs_is_closed', 'after': 'close_heatswitch'},
            {'trigger': 'close', 'source': 'closed', 'dest': None, 'conditions': 'hs_is_closed'},

            {'trigger': 'next', 'source': 'closing', 'dest': 'closed', 'conditions': 'hs_is_closed'},
            {'trigger': 'next', 'source': 'closing', 'dest': None, 'unless': 'hs_is_closed', 'after': 'close_heatswitch'},
        ]

        states = (State('opened', on_enter='record_entry'),
                  State('opening', on_enter='record_entry'),
                  State('closed', on_enter='record_entry'),
                  State('closing', on_enter='record_entry'))

        hs = HeatswitchMotor('/dev/heaswitch')
        # hs = SimHeatswitch()

        hs.monitor(QUERY_INTERVAL, hs.motor_position, value_callback=monitor_callback)

        self.hs = hs
        self.lock = threading.RLock()
        self._run = False  # Set to false to kill the main loop
        self._mainthread = None

        # initial = compute_initial_state(self.hs, self.statefile)
        if self.hs.last_recorded_position == self.hs.max_position:
            initial = "closed"
        elif self.hs.last_recorded_position == self.hs.min_position:
            initial = "opened"
        else:
            if np.random.randint(0, 1000) % 2 == 0:
                initial = "opening"
            else:
                initial = "closing"
        self.state_entry_time = {initial: time.time()}
        LockedMachine.__init__(self, transitions=transitions, initial=initial, states=states,
                               machine_context=self.lock, send_event=True)

        self.start_main()

    def open_heatswitch(self, event):
        new_pos = self.hs.move_by(-50000)
        return new_pos

    def close_heatswitch(self, event):
        new_pos = self.hs.move_by(50000)
        return new_pos

    def hs_is_opened(self, event):
        return self.hs.last_recorded_position == FULL_OPEN_POSITION

    def hs_is_closed(self, event):
        return self.hs.last_recorded_position == FULL_CLOSE_POSITION

    def _main(self):
        while self._run:
            try:
                self.next()
                log.debug(f"Heatswitch state is: {self.state}")
            except IOError:
                print("IOError")
                log.error(exc_info=True)
            except MachineError:
                print("MachineError")
                log.error(exc_info=True)
            except RedisError:
                print("RedisError")
                log.error(exc_info=True)
            finally:
                time.sleep(self.LOOP_INTERVAL)

    def start_main(self):
        self._run = True  # Set to false to kill the m
        self._mainthread = threading.Thread(target=self._main)
        self._mainthread.daemon = True
        self._mainthread.start()

    def record_entry(self, event):
        self.state_entry_time[self.state] = time.time()
        log.info(f"Recorded entry: {self.state}")
        redis.store({HS_POS: self.state})
        # write_persisted_state(self.statefile, self.state)


if __name__ == "__main__":

    # Library.enable_device_db_store()
    redis = MKIDRedis(create_ts_keys=TS_KEYS)
    util.setup_logging('heatswitchAgent')

    controller = HeatswitchController()

    try:
        while True:
            for key, val in redis.listen(COMMAND_KEYS):
                log.debug(f"Redis listened to something! Key: {key} -- Val: {val}")
                hspos = val.lower()
                if hspos == 'open':
                    controller.open()
                elif hspos == 'close':
                    controller.close()
    except RedisError as e:
        log.error(f"Redis server error! {e}")