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
from collections import defaultdict
import time
import logging
import threading
import numpy as np
from transitions import MachineError, State
from transitions.extensions import LockedMachine

from mkidcontrol.mkidredis import MKIDRedis, RedisError
import mkidcontrol.util as util
from mkidcontrol.devices import HeatswitchPosition, write_persisted_state, load_persisted_state
from zaber_motion import Library
from zaber_motion.binary import Connection, BinarySettings, CommandCode

QUERY_INTERVAL = 1

log = logging.getLogger(__name__)

DEFAULT_MAX_VELOCITY = 3e3
DEFAULT_RUNNING_CURRENT = 13
DEFAULT_ACCELERATION = 2

FULL_OPEN_POSITION = 0
FULL_CLOSE_POSITION = 4194303

STATUS_KEY = 'status:device:heatswitch:status'  # OK | ERROR | OFF
HEATSWITCH_POSITION_KEY = "status:device:heatswitch:position"  # OPENED | OPENING | CLOSED | CLOSING
MOTOR_POS = "status:device:heatswitch:motor-position"  # Integer between 0 and 4194303

HEATSWITCH_MOVE_KEY = f"command:{HEATSWITCH_POSITION_KEY}"
HEATSWITCH_MOVE_BY_KEY = f"command:device:heatswitch:move-by"
HEATSWITCH_ENGINEERING_MODE_KEY = f"command:device:heatswitch:engineering-mode"
HEATSWITCH_SET_POSITION_KEY = f"command:device:heatswitch:reset-position"

COMMAND_KEYS = (HEATSWITCH_MOVE_KEY, HEATSWITCH_MOVE_BY_KEY, HEATSWITCH_ENGINEERING_MODE_KEY, HEATSWITCH_SET_POSITION_KEY)
TS_KEYS = (MOTOR_POS,)


def close():
    redis.publish(HEATSWITCH_MOVE_KEY, HeatswitchPosition.CLOSE, store=False)


def open():
    redis.publish(HEATSWITCH_MOVE_KEY, HeatswitchPosition.OPEN, store=False)


def is_opened():
    return (redis.read(HEATSWITCH_POSITION_KEY) == HeatswitchPosition.OPENED) or (redis.read(HEATSWITCH_POSITION_KEY) == HeatswitchPosition.OPENING)


def is_closed():
    return redis.read(HEATSWITCH_POSITION_KEY) == HeatswitchPosition.CLOSED


def monitor_callback(mpos):
    d = {MOTOR_POS: mpos}
    try:
        if mpos is None:
            # N.B. If there is an error on the query, the value passed is None
            redis.store({STATUS_KEY: "Error"})
        else:
            redis.store(d, timeseries=True)
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
    def __init__(self, port, timeout=(4194303 * 1.25)/3e3, set_mode=False):
        c = Connection.open_serial_port(port)
        self.hs = c.detect_devices()[0]

        self.initialized = False
        self.last_recorded_position = None
        self.last_move = 0

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

    @property
    def state(self):
        if self.motor_position() == FULL_CLOSE_POSITION:
            return HeatswitchPosition.CLOSED
        elif self.motor_position() == FULL_OPEN_POSITION:
            return HeatswitchPosition.OPENED
        else:
            if self.last_move >= 0:
                return HeatswitchPosition.CLOSING
            else:
                return HeatswitchPosition.OPENING

    def motor_position(self):
        for i in range(5):
            try:
                position = self.hs.get_position()
                log.debug(f"Motor has reported that it is at position {position}")
                return position
            except Exception as e:
                log.debug(f"Error in querying heat switch motor. Attempt {i+1} of 5 failed. Trying again.")

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
                    new_pos = self.hs.move_relative(new_dist)
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
                new_pos = self.hs.move_relative(dist)
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

    def _set_position_value(self, value):
        """
        Tells this heat switch that it is at a different position than it thinks it is
        E.G. If the heatswitch reports that it is at 0 and one uses this function with <value>=10, it will then report
        that it is at position 10 without ever having moved.
        THIS IS SOLELY AN ENGINEERING FUNCTION AND SHOULD ONLY BE USED WITH EXTREME CARE
        """
        self.hs.generic_command(CommandCode.SET_CURRENT_POSITION, value)

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


class HeatswitchController(LockedMachine):
    LOOP_INTERVAL = 1
    BLOCKS = defaultdict(set)

    def __init__(self, heatswitch, statefile='./heatswitchmotorstate.txt'):
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

            {'trigger': 'stop', 'source': '*', 'dest': 'off'},
            {'trigger': 'next', 'source': 'engineering', 'dest': None},
            {'trigger': 'open', 'source': 'engineering', 'dest': 'opening'},
            {'trigger': 'close', 'source': 'engineering', 'dest': 'closing'}
        ]

        states = (State('opened', on_enter='record_entry'),
                  State('opening', on_enter='record_entry'),
                  State('closed', on_enter='record_entry'),
                  State('closing', on_enter='record_entry'),
                  State('engineering', on_enter='record_entry'))

        self.hs = heatswitch
        self.lock = threading.RLock()
        self._run = False  # Set to false to kill the main loop
        self._mainthread = None
        self.statefile = statefile

        initial = compute_initial_state(self.hs)
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
        redis.store({HEATSWITCH_POSITION_KEY: self.state})
        redis.store({STATUS_KEY: 'OK'})
        write_persisted_state(self.statefile, self.state)


import wtforms
from wtforms.fields import *
from wtforms.widgets import HiddenInput
from wtforms.fields.html5 import *
from wtforms.validators import *
from wtforms import Form
from flask_wtf import FlaskForm
from serial import SerialException


class HeatSwitchForm(FlaskForm):
    open = SubmitField("Open")
    close = SubmitField("Close")
    engineering_mode = SubmitField("Engineering Mode")
    move_by = IntegerField("Move By", default=0, validators=[number_range(-1 * FULL_CLOSE_POSITION, FULL_CLOSE_POSITION)], render_kw={'disabled': True})
    current_position = IntegerField("Current Position", default=FULL_CLOSE_POSITION, validators=[number_range(FULL_OPEN_POSITION, FULL_CLOSE_POSITION)], render_kw={'disabled': True})
    set_position = IntegerField("Set Position To:", default=FULL_CLOSE_POSITION, validators=[number_range(FULL_OPEN_POSITION, FULL_CLOSE_POSITION)], render_kw={'disabled': True})


class HeatSwitchEngineeringModeForm(FlaskForm):
    open = SubmitField("Open")
    close = SubmitField("Close")
    engineering_mode = SubmitField("Engineering Mode", render_kw={'disabled': True})
    move_by = IntegerField("Move By", default=0, validators=[number_range(-1 * FULL_CLOSE_POSITION, FULL_CLOSE_POSITION)])
    current_position = IntegerField("Current Position", default=FULL_CLOSE_POSITION, validators=[number_range(FULL_OPEN_POSITION, FULL_CLOSE_POSITION)])
    set_position = IntegerField("Set Position To:", default=FULL_CLOSE_POSITION, validators=[number_range(FULL_OPEN_POSITION, FULL_CLOSE_POSITION)])


if __name__ == "__main__":

    redis = MKIDRedis(ts_keys=TS_KEYS)
    util.setup_logging('heatswitchAgent')

    try:
        hs = HeatswitchMotor('/dev/heatswitch')
        redis.store({STATUS_KEY: "OK"})
    except Exception as e:
        # TODO: There is a trivial exception that will trigger this: If you change a redis DB key that is necessary for
        #  the heatswitch to connect, then you will error out.
        log.critical(f"Could not connect to the heatswitch!")
        redis.store({STATUS_KEY: f"Error: {e}"})
        sys.exit(1)

    hs.monitor(QUERY_INTERVAL, (hs.motor_position,), value_callback=monitor_callback)

    controller = HeatswitchController(heatswitch=hs)

    try:
        while True:
            for key, val in redis.listen(COMMAND_KEYS):
                log.debug(f"Redis listened to something! Key: {key} -- Val: {val}")
                try:
                    if key == HEATSWITCH_MOVE_KEY:
                        hspos = val.lower()
                        if hspos == 'open':
                            controller.open()
                        elif hspos == 'close':
                            controller.close()
                    elif key == HEATSWITCH_ENGINEERING_MODE_KEY:
                        log.info(f"Entering engineering mode: be warned you are in manual territory!")
                        controller.stop()
                    elif key == HEATSWITCH_SET_POSITION_KEY:
                        log.info(f"Setting registered position of the heatswitch motor to {val}")
                        pos = val
                        hs._set_position_value(pos)
                    elif key == HEATSWITCH_MOVE_BY_KEY:
                        log.info(f"Attempting to move heatswitch motor by {val} steps")
                        dist = val
                        hs.move_by(dist)
                    else:
                        log.warning(f"Heard: '{key} -- {val}, not supported commands")
                    redis.store({STATUS_KEY: "OK"})
                except (IOError, MachineError) as e:
                    redis.store({STATUS_KEY: f"Error: {e}"})
    except RedisError as e:
        log.error(f"Redis server error! {e}")
        sys.exit(1)