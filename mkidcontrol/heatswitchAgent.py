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

TODO: Test hysteresis
TODO: Serial error handling
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
from mkidcontrol.commands import COMMANDSHS, SimCommand

QUERY_INTERVAL = 1

log = logging.getLogger(__name__)

SETTING_KEYS = tuple(COMMANDSHS.keys())

DEFAULT_MAX_VELOCITY = 3e3  # Maximum velocity empirically found with ARCONS
DEFAULT_RUNNING_CURRENT = 18  # Current can be set between 10 (highest) and 127 (lowest). Lower current (higher number)
# will avoid damaging the heat switch if limit is reached by mistake
DEFAULT_ACCELERATION = 2  # Default acceleration from ARCONS
FULL_OPEN_POSITION = 0  # Hard limit of the motor opening
FULL_CLOSE_POSITION = 4194303  # Halfway point for motor position, physical hard stop with clamps closed on heat sinks

STATUS_KEY = 'status:device:heatswitch:status'  # OK | ERROR | OFF
HEATSWITCH_POSITION_KEY = "status:device:heatswitch:position"  # opened | opening | closed | closing
MOTOR_POS = "status:device:heatswitch:motor:position"  # Integer between 0 and 4194303

HEATSWITCH_MOVE_KEY = "device-settings:heatswitch:position"
STEP_SIZE_KEY = "device-settings:heatswitch:step-size"
OPERATING_MODE_KEY = "device-settings:heatswitch:operating-mode"
VELOCITY_KEY = "device-settings:heatswitch:max-velocity"
RUNNING_CURRENT_KEY = "device-settings:heatswitch:running-current"
ACCELERATION_KEY = "device-settings:heatswitch:acceleration"
MOVE_BY_KEY = f"device:heatswitch:motor:desired-move"
MOVE_TO_KEY = f"device:heatswitch:motor:desired-position"
SET_POSITION_KEY = f"device:heatswitch:motor:reset-position"
SET_STATE_KEY = f"device:heatswitch:reset-state"

COMMAND_KEYS = [f"command:{k}" for k in SETTING_KEYS]
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
            {'trigger': 'next', 'source': 'off', 'dest': None},
            {'trigger': 'open', 'source': 'off', 'dest': 'opening'},
            {'trigger': 'close', 'source': 'off', 'dest': 'closing'}
        ]

        states = (State('opened', on_enter='record_entry'),
                  State('opening', on_enter='record_entry'),
                  State('closed', on_enter='record_entry'),
                  State('closing', on_enter='record_entry'),
                  State('off', on_enter='record_entry'))

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
        new_pos = self.hs.move_by(-1 * redis.read(STEP_SIZE_KEY))
        return new_pos

    def close_heatswitch(self, event):
        new_pos = self.hs.move_by(redis.read(STEP_SIZE_KEY))
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


class HeatSwitchForm2(FlaskForm):
    open = SubmitField("Open")
    close = SubmitField("Close")


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
        log.critical(f"Could not connect to the heatswitch! Error {e}")
        redis.store({STATUS_KEY: f"Error: {e}"})
        sys.exit(1)
    except RedisError as e:
        log.error(f"Redis server error! {e}")
        sys.exit(1)

    hs.monitor(QUERY_INTERVAL, (hs.motor_position,), value_callback=monitor_callback)

    if redis.read(OPERATING_MODE_KEY) == "regular":
        controller = HeatswitchController(heatswitch=hs)
    else:
        controller = None

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
                            if redis.read(OPERATING_MODE_KEY) == "regular":
                                if val == "open":
                                    controller.open()
                                elif val == "close":
                                    controller.close()
                                else:
                                    log.warning("Illegal command that was not previously handled!")
                            else:
                                log.warning(f"Trying to perform a normal command on heatswitch outside of "
                                            f"regular operation mode! Ignoring")
                        elif key == OPERATING_MODE_KEY:
                            if val == "engineering":
                                controller.off()
                                controller = None
                            elif val == "regular":
                                controller = HeatswitchController(heatswitch=hs)
                        elif key in [VELOCITY_KEY, RUNNING_CURRENT_KEY, ACCELERATION_KEY]:
                            hs.update_binary_setting(key, val)
                        elif key in [MOVE_BY_KEY, MOVE_TO_KEY, SET_POSITION_KEY, SET_STATE_KEY]:
                            if redis.read(OPERATING_MODE_KEY) == "regular":
                                log.warning(f"Trying to perform an engineering command on heatswitch outside of engineering mode! Ignoring")
                            else:
                                if key == MOVE_BY_KEY:
                                    hs.move_by(val)
                                elif key == MOVE_TO_KEY:
                                    hs.move_to(val)
                                elif key == SET_POSITION_KEY:
                                    hs._set_position_value(val)
                                elif key == SET_STATE_KEY:
                                    redis.store({HEATSWITCH_POSITION_KEY: val})
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