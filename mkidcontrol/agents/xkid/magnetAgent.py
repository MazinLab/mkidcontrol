"""
Author: Noah Swimmer
19 July 2022

Program for controlling the magnet. The magnet controller itself is a statemachine but requires no instruments to run.
It will run even with no lakeshore/heatswitch/etc., although it will not allow anything to actually happen.

# TODO: Test on actual magnet

# TODO: Double check all logic works and each instrument is told what it needs to do properly
"""

import sys
import time
import logging
import threading
from datetime import datetime, timedelta
import numpy as np
from collections import defaultdict
from transitions import MachineError, State
from transitions.extensions import LockedMachine, LockedGraphMachine
import pkg_resources

from mkidcontrol.devices import write_persisted_state, load_persisted_state
from mkidcontrol.mkidredis import RedisError
import mkidcontrol.util as util
import mkidcontrol.mkidredis as redis
from mkidcontrol.commands import LakeShoreCommand, COMMANDSMAGNET
import mkidcontrol.agents.xkid.heatswitchAgent as heatswitch
import mkidcontrol.agents.lakeshore372Agent as ls372
import mkidcontrol.agents.lakeshore625Agent as ls625

MAX_PERSISTED_STATE_LIFE_SECONDS = 3600

SETTING_KEYS = tuple(COMMANDSMAGNET.keys())

SOAK_TIME_KEY = 'device-settings:magnet:soak-time'
SOAK_CURRENT_KEY = 'device-settings:magnet:soak-current'
RAMP_RATE_KEY = 'device-settings:magnet:ramp-rate'
DERAMP_RATE_KEY = 'device-settings:magnet:deramp-rate'
COOLDOWN_SCHEDULED_KEY = 'device-settings:magnet:cooldown-scheduled'
SCHEDULED_COOLDOWN_TIMESTAMP_KEY = 'device-settings:magnet:cooldown-scheduled:timestamp'

IMPOSE_UPPER_LIMIT_ON_REGULATION_KEY = 'device-settings:magnet:enable-temperature-regulation-upper-limit'
STATEFILE_PATH_KEY = 'device-settings:magnet:statefile'  # /mkidcontrol/mkidcontrol/logs/statefile.txt

STOP_RAMP_KEY = 'device-settings:ls625:stop-current-ramp'
COLD_AT_CMD = 'be-cold-at'
COLD_NOW_CMD = 'get-cold'
ABORT_CMD = 'abort-cooldown'
CANCEL_COOLDOWN_CMD = 'cancel-scheduled-cooldown'
QUENCH_KEY = 'event:quenching'

MAGNET_COMMAND_KEYS = tuple([COLD_AT_CMD, COLD_NOW_CMD, ABORT_CMD, CANCEL_COOLDOWN_CMD, STOP_RAMP_KEY])

MAGNET_STATE_KEY = 'status:magnet:state'  # Names from statemachine
MAGNET_CURRENT_KEY = 'status:magnet:current'
MAGNET_FIELD_KEY = 'status:magnet:field'
CONTROLLER_STATUS_KEY = 'status:magnet:status'

TS_KEYS = [MAGNET_CURRENT_KEY, MAGNET_FIELD_KEY]

COMMAND_KEYS = [f"command:{k}" for k in list(MAGNET_COMMAND_KEYS) + list(SETTING_KEYS)]

DEVICE_TEMP_KEY = 'status:temps:device-stage:temp'
REGULATION_TEMP_KEY = "device-settings:magnet:regulating-temp"
LAKESHORE_SETPOINT_KEY = 'device-settings:ls372:heater-channel-0:setpoint'

log = logging.getLogger("magentAgent")


class StateError(Exception):
    pass


def compute_initial_state(statefile):
    # TODO: Check legality and test logic
    initial_state = 'deramping'  # always safe to start here
    redis.store({COOLDOWN_SCHEDULED_KEY: 'no'})
    redis.store({SCHEDULED_COOLDOWN_TIMESTAMP_KEY: ''})
    try:
        if ls625.is_initialized():
            state_time, persisted_state = load_persisted_state(statefile)
            if (persisted_state is None) or (time.time()-state_time > MAX_PERSISTED_STATE_LIFE_SECONDS):
                return initial_state
            else:
                initial_state = persisted_state
            current = ls625.lakeshore_current()
            if initial_state == 'soaking' and (current >= 0.96 * float(redis.read(SOAK_CURRENT_KEY))) and (current <= 1.04 * float(redis.read(SOAK_CURRENT_KEY))):
                initial_state = 'ramping'  # we can recover

            # be sure the command is sent
            if initial_state in ('hs_closing', 'ramping', 'soaking', 'off'):
                pass
                # heatswitch.close()

            if initial_state in ('hs_opening', 'cooling', 'regulating'):
                pass
                # heatswitch.open()

            # failure cases
            if ((initial_state in ('ramping', 'soaking') and heatswitch.is_opened()) or
                    (initial_state in ('cooling', 'regulating') and heatswitch.is_closed())):
                initial_state = 'deramping'  # deramp to off, we are out of sync with the hardware

    except IOError:
        log.critical('Lost ls625 connection during agent startup. defaulting to deramping')
        initial_state = 'deramping'
    except RedisError:
        log.critical('Lost redis connection during compute_initial_state startup.')
        raise
    log.info(f"\n\n------ Initial State is: {initial_state} ------\n")
    return initial_state


# class Magnet():
#     pass
#
# m = Magnet()
#
# t = [{'trigger': 'abort', 'source': '*', 'dest': 'start_deramping'},
#     {'trigger': 'quench', 'source': '*', 'dest': 'off', 'prepare': 'kill_current'},
#     {'trigger': 'start', 'source': 'off', 'dest': 'hs_closing',
#     'prepare': 'close_heatswitch'},
#     {'trigger': 'start', 'source': 'deramping', 'dest': 'hs_closing',
#     'prepare': 'close_heatswitch'},
#     {'trigger': 'next', 'source': 'hs_closing', 'dest': 'start_ramping', 'conditions': 'heatswitch_closed'},
#     {'trigger': 'next', 'source': 'hs_closing', 'dest': None, 'prepare': 'close_heatswitch'},
#     {'trigger': 'next', 'source': 'start_ramping', 'dest': None, 'before': 'begin_ramp_up'},
#     {'trigger': 'next', 'source': 'start_ramping', 'dest': 'ramping', 'conditions': 'ramp_ok'},
#     {'trigger': 'next', 'source': 'ramping', 'dest': None, 'unless': 'current_ready_to_soak',
#     'conditions': 'ramp_ok'},
#     {'trigger': 'next', 'source': 'ramping', 'dest': 'soaking', 'conditions': 'current_ready_to_soak'},
#     {'trigger': 'next', 'source': 'soaking', 'dest': None, 'unless': 'soak_time_expired',
#     'conditions': 'current_at_soak'},
#     {'trigger': 'next', 'source': 'soaking', 'dest': 'hs_opening',
#     'prepare': ('open_heatswitch', 'ls372_to_pid'),
#     'conditions': ('current_at_soak', 'soak_time_expired')},
#     {'trigger': 'next', 'source': 'soaking', 'dest': 'start_deramping'},
#     {'trigger': 'next', 'source': 'hs_opening', 'dest': 'start_cooling',
#     'conditions': ('heatswitch_opened', 'ls372_in_pid')},
#     {'trigger': 'next', 'source': 'hs_opening', 'dest': None,
#     'prepare': ('open_heatswitch', 'ls372_to_pid')},
#     {'trigger': 'next', 'source': 'start_cooling', 'dest': None, 'before': 'begin_ramp_down'},
#     {'trigger': 'next', 'source': 'start_cooling', 'dest': 'cooling', 'conditions': 'deramp_ok'},
#     {'trigger': 'next', 'source': 'cooling', 'dest': None, 'unless': 'device_ready_for_regulate',
#     'conditions': ('heatswitch_opened', 'deramp_ok')},
#     {'trigger': 'next', 'source': 'cooling', 'dest': 'regulating', 'before': 'ls372_to_pid',
#     'conditions': ('heatswitch_opened', 'ls372_in_pid', 'deramp_ok')},
#     {'trigger': 'next', 'source': 'cooling', 'dest': 'start_deramping', 'conditions': 'heatswitch_closed'},
#     {'trigger': 'next', 'source': 'regulating', 'dest': None,
#     'conditions': ('device_regulatable', 'ls372_in_pid')},
#     {'trigger': 'next', 'source': 'regulating', 'dest': 'start_deramping'},
#     {'trigger': 'next', 'source': 'start_deramping', 'dest': None, 'prepare': 'begin_ramp_down'},
#     {'trigger': 'next', 'source': 'start_deramping', 'dest': 'deramping', 'conditions': 'deramp_ok'},
#     {'trigger': 'next', 'source': 'deramping', 'dest': None, 'unless': 'current_off',
#     'prepare': 'begin_ramp_down'},
#     {'trigger': 'next', 'source': 'deramping', 'dest': 'off'},
#     {'trigger': 'next', 'source': 'off', 'dest': None}
#     ]
#
# s = (State('off', on_enter=['record_entry', 'kill_current']),
#     State('hs_closing', on_enter='record_entry'),
#     State('start_ramping', on_enter='record_entry'),
#     State('ramping', on_enter='record_entry'),
#     State('soaking', on_enter='record_entry'),
#     State('hs_opening', on_enter='record_entry'),
#     State('start_cooling', on_enter='record_entry'),
#     State('cooling', on_enter='record_entry'),
#     State('regulating', on_enter='record_entry'),
#     State('start_deramping', on_enter='record_entry'),
#     # Entering ramping MUST succeed
#     State('deramping', on_enter='record_entry'))
#
# mach = LockedGraphMachine(model=m, transitions=t, states=s)

class MagnetController(LockedMachine):
    LOOP_INTERVAL = 1
    BLOCKS = defaultdict(set)  # This holds the ls625 commands that are blocked out in a given state
    MAX_CURRENT = 9.44  # Amps

    def __init__(self, statefile='./magnetstate.txt'):
        transitions = [
            # Allow aborting from any point, trigger will always succeed
            {'trigger': 'abort', 'source': '*', 'dest': 'start_deramping'},

            # Allow quench (direct to hard off) from any point, trigger will always succeed
            {'trigger': 'quench', 'source': '*', 'dest': 'off'},

            # Allow starting a ramp from off or deramping, if close_heatswitch fails then start should fail
            {'trigger': 'start', 'source': 'off', 'dest': 'hs_closing',
             'prepare': 'close_heatswitch'},
            {'trigger': 'start', 'source': 'deramping', 'dest': 'hs_closing',
             'prepare': 'close_heatswitch'},

            # Transitions for cooldown progression

            # stay in hs_closing until it is closed then transition to ramping
            # if we can't get the status from redis then the conditions default to false and we stay put
            {'trigger': 'next', 'source': 'hs_closing', 'dest': 'start_ramping', 'conditions': 'heatswitch_closed'},
            {'trigger': 'next', 'source': 'hs_closing', 'dest': None, 'prepare': 'close_heatswitch'},

            {'trigger': 'next', 'source': 'start_ramping', 'dest': None, 'before': 'begin_ramp_up', 'unless': 'ramp_ok'},
            {'trigger': 'next', 'source': 'start_ramping', 'dest': 'ramping', 'conditions': 'ramp_ok'},

            # stay in ramping, as long as the ramp is going OK
            {'trigger': 'next', 'source': 'ramping', 'dest': None, 'unless': 'current_ready_to_soak',
             'conditions': 'ramp_ok'},
            {'trigger': 'next', 'source': 'ramping', 'dest': 'soaking', 'conditions': 'current_ready_to_soak'},

            # stay in soaking until we've elapsed the soak time, if the current changes move to deramping as something
            # is quite wrong, when elapsed command heatswitch open and move to waiting on the heatswitch
            # if we can't get the current then conditions raise IOerrors and we will deramp
            # if we can't get the settings from redis then the conditions default to false and we stay put
            # Note that the hs_opening command will always complete (even if it fails) so the state will progress
            {'trigger': 'next', 'source': 'soaking', 'dest': None, 'unless': 'soak_time_expired',
             'conditions': 'current_at_soak'},
            {'trigger': 'next', 'source': 'soaking', 'dest': 'hs_opening',
             'prepare': ('open_heatswitch', 'ls372_to_pid'),
             'conditions': ('current_at_soak', 'soak_time_expired')},
            # condition repeated to preclude call passing due to IO hiccup
            {'trigger': 'next', 'source': 'soaking', 'dest': 'start_deramping'},

            # stay in hs_opening until it is open then transition to cooling
            # don't require conditions on current
            # if we can't get the status from redis then the conditions default to false and we stay put
            # {'trigger': 'next', 'source': 'hs_opening', 'dest': 'cooling',
            #  'conditions': ('heatswitch_opened', 'ls372_in_pid')},
            {'trigger': 'next', 'source': 'hs_opening', 'dest': 'start_cooling',
             'conditions': ('heatswitch_opened', 'ls372_in_pid')},
            {'trigger': 'next', 'source': 'hs_opening', 'dest': None,
             'prepare': ('open_heatswitch', 'ls372_to_pid')},

            {'trigger': 'next', 'source': 'start_cooling', 'dest': None, 'before': 'begin_ramp_down'},
            {'trigger': 'next', 'source': 'start_cooling', 'dest': 'cooling', 'conditions': 'deramp_ok'},

            # stay in cooling, decreasing the current a bit until the device is regulatable
            # if the heatswitch closes move to deramping
            # if we can't change the current or interact with redis for related settings the its a noop and we
            #  stay put
            # if we can't put the device in pid mode (IOError)  we stay put
            {'trigger': 'next', 'source': 'cooling', 'dest': None, 'unless': 'device_ready_for_regulate',
             'conditions': ('heatswitch_opened', 'deramp_ok')},
            {'trigger': 'next', 'source': 'cooling', 'dest': 'regulating', 'before': 'ls372_to_pid',
             'conditions': ('heatswitch_opened', 'ls372_in_pid', 'deramp_ok')},
            {'trigger': 'next', 'source': 'cooling', 'dest': 'start_deramping', 'conditions': 'heatswitch_closed'},

            # stay in regulating until the device is too warm to regulate
            # if it somehow leaves PID mode (or we can't verify it is in PID mode: IOError) move to deramping
            # if we cant pull the temp from redis then device is assumed unregulatable and we move to deramping
            {'trigger': 'next', 'source': 'regulating', 'dest': None,
             'conditions': ('device_regulatable', 'ls372_in_pid')},
            {'trigger': 'next', 'source': 'regulating', 'dest': 'start_deramping'},

            {'trigger': 'next', 'source': 'start_deramping', 'dest': None, 'prepare': 'begin_ramp_down'},
            {'trigger': 'next', 'source': 'start_deramping', 'dest': 'deramping', 'conditions': 'deramp_ok'},

            # stay in deramping, trying to decrement the current, until the device is off then move to off
            # condition defaults to false in the even of an IOError and decrement_current will just noop if there are
            # failures
            {'trigger': 'next', 'source': 'deramping', 'dest': None, 'unless': 'current_off',
             'prepare': 'begin_ramp_down'},
            {'trigger': 'next', 'source': 'deramping', 'dest': 'off'},

            # once off stay put, if the current gets turned on while in off then something is fundamentally wrong with
            # the sim itself. This can't happen.
            {'trigger': 'next', 'source': 'off', 'dest': None}
        ]

        states = (  # Entering off MUST succeed
            State('off', on_enter=['record_entry', 'kill_current']),
            State('hs_closing', on_enter='record_entry'),
            State('start_ramping', on_enter='record_entry'),
            State('ramping', on_enter='record_entry'),
            State('soaking', on_enter='record_entry'),
            State('hs_opening', on_enter='record_entry'),
            State('start_cooling', on_enter='record_entry'),
            State('cooling', on_enter='record_entry'),
            State('regulating', on_enter='record_entry'),
            State('start_deramping', on_enter='record_entry'),
            # Entering ramping MUST succeed
            State('deramping', on_enter='record_entry'))

        self.last_5_currents = []
        self.statefile = statefile
        self.lock = threading.RLock()
        self.scheduled_cooldown = None
        self._run = False  # Set to false to kill the main loop
        self._mainthread = None

        initial = compute_initial_state(self.statefile)
        self.state_entry_time = {initial: time.time()}
        LockedMachine.__init__(self, transitions=transitions, initial=initial, states=states, machine_context=self.lock,
                               send_event=True)

        self.start_main()

    def start_main(self):
        self._run = True  # Set to false to kill the m
        self._mainthread = threading.Thread(target=self._main)
        self._mainthread.daemon = True
        self._mainthread.start()

    def _main(self):
        while self._run:
            try:
                self.last_5_currents.append(float(redis.read(MAGNET_CURRENT_KEY)[1]))
                self.last_5_currents = self.last_5_currents[-5:]
                self.next()
                log.debug(f"Magnet state is: {self.state}")
            except IOError:
                log.info(exc_info=True)
            except MachineError:
                log.info(exc_info=True)
            except RedisError:
                log.info(exc_info=True)
            finally:
                time.sleep(self.LOOP_INTERVAL)

    @property
    def min_time_until_cool(self):
        """
        return an estimate of the time to cool from the current state
        """
        soak_current = float(redis.read(SOAK_CURRENT_KEY))
        soak_time = float(redis.read(SOAK_TIME_KEY)) * 60  # Soak time stored in minues, must be in seconds
        ramp_rate = float(redis.read(RAMP_RATE_KEY))
        deramp_rate = -1 * float(redis.read(DERAMP_RATE_KEY))  # Deramp rate is stored as a POSITIVE number
        current_current = self.last_5_currents[-1]
        current_state = self.state  # NB: If current_state is regulating time_to_cool will return 0 since it is already cool.

        time_to_cool = 0
        if current_state in ('ramping', 'off', 'hs_closing', 'start_ramping'):
            time_to_cool = ((soak_current - current_current) / ramp_rate) + soak_time + (
                    (0 - soak_current) / deramp_rate)
        if current_state in ('soaking', 'hs_opening'):
            time_to_cool = (time.time() - self.state_entry_time['soaking']) + ((0 - soak_current) / deramp_rate)
        if current_state in ('cooling', 'deramping', 'start_cooling', 'start_deramping'):
            time_to_cool = -1 * current_current / deramp_rate

        return timedelta(seconds=time_to_cool)

    def schedule_cooldown(self, time):
        """time specifies the time by which to be cold"""
        # TODO how to handle scheduling when we are warming up or other such
        if self.state not in ('off', 'deramping'):
            raise ValueError(f'Cooldown in progress, abort before scheduling.')

        now = datetime.now()
        time_needed = self.min_time_until_cool

        if time < now + time_needed:
            raise ValueError(
                f'Time travel not possible, specify a time at least {time_needed} in the future. (Current time: {now.timestamp()})')

        self.cancel_scheduled_cooldown()
        redis.store({COOLDOWN_SCHEDULED_KEY: 'no'})
        redis.store({SCHEDULED_COOLDOWN_TIMESTAMP_KEY: ''})
        t = threading.Timer((time - time_needed - now).seconds, self.start)  # TODO (For JB): self.start?
        self.scheduled_cooldown = (time - time_needed, t)

        redis.store({COOLDOWN_SCHEDULED_KEY: 'yes'})
        redis.store({SCHEDULED_COOLDOWN_TIMESTAMP_KEY: f"{time.timestamp()}"})
        t.daemon = True
        t.start()

    def cancel_scheduled_cooldown(self):
        if self.scheduled_cooldown is not None:
            log.info(f'Cancelling cooldown scheduled for {self.scheduled_cooldown[0]}')
            self.scheduled_cooldown[1].cancel()
            self.scheduled_cooldown = None
        else:
            log.debug(f'No pending cooldown to cancel')

    @property
    def status(self):
        """A string indicating the current status e.g. state[, Cooldown scheduled for X] """
        ret = self.state
        if ret not in ('off', 'regulating'):
            ret += f", cold in {self.min_time_until_cool} minutes"
        if self.scheduled_cooldown is not None:
            ret += f', cooldown scheduled for {self.scheduled_cooldown[0]}'
        return ret

    def close_heatswitch(self, event):
        try:
            heatswitch.close()
        except RedisError:
            pass

    def open_heatswitch(self, event):
        try:
            heatswitch.open()
        except RedisError:
            pass

    def current_off(self, event):
        try:
            # return redis.read('device-settings:ls625:control-mode') == "Sum" and \
            #        float(redis.read('device-settings:ls625:desired-current')) == 0.0 and \
            #        abs(float(redis.read(MAGNET_CURRENT_KEY)[1])) <= 0.005
            return float(redis.read('device-settings:ls625:desired-current')) == 0.0 and \
                   abs(float(redis.read(MAGNET_CURRENT_KEY)[1])) <= 0.005
        except IOError:
            return False

    def heatswitch_closed(self, event):
        """return true iff heatswitch is closed"""
        try:
            return heatswitch.is_closed()
        except RedisError:
            return False

    def heatswitch_opened(self, event):
        """return true iff heatswitch is closed"""
        try:
            return heatswitch.is_opened()
        except RedisError:
            return False

    def ls372_to_pid(self, event):
        try:
            ls372.to_pid_output()
        except RedisError:
            pass

    def ls372_to_no_output(self, event):
        try:
            ls372.to_no_output()
        except RedisError:
            pass

    def ls372_in_pid(self, event):
        try:
            return ls372.in_pid_output()
        except RedisError:
            return False

    def ls372_in_no_output(self, event):
        try:
            return ls372.in_no_output()
        except RedisError:
            return False

    def begin_ramp_up(self, event):
        soak_current = None
        try:
            soak_current = abs(float(redis.read(SOAK_CURRENT_KEY)))
        except RedisError:
            log.warning(f"Unable to pull {SOAK_CURRENT_KEY}, using default value of {self.MAX_CURRENT}")

        try:
            if soak_current:
                ls625.start_ramp_up(soak_current)
            else:
                ls625.start_ramp_up()
        except Exception:
            log.warning(f"Cycle could not be started! {e}")

    def begin_ramp_down(self, event):
        try:
            ls625.start_ramp_down(0)
        except Exception as e:
            log.warning(f"Ramp down could not be started! {e}")

    def ramp_ok(self, event):
        currents = self.last_5_currents
        steps = np.diff(currents)
        if np.sum(steps > 0) >= 3:
            return True
        elif currents[-1] >= currents[-2]:
            return True
        else:
            return False

    def deramp_ok(self, event):
        currents = self.last_5_currents
        steps = np.diff(currents)
        if np.all(steps <= 0):
            return True
        elif np.sum(steps) >= 3:
            return True
        elif currents[-1] <= currents[-2]:
            return True
        else:
            return False

    def soak_time_expired(self, event):
        try:
            return (time.time() - self.state_entry_time['soaking']) >= (float(redis.read(SOAK_TIME_KEY)) * 60)
        except RedisError:
            return False

    def current_ready_to_soak(self, event):
        try:
            current = float(redis.read(MAGNET_CURRENT_KEY)[1])
            soak_current = float(redis.read(SOAK_CURRENT_KEY))
            diff = (current - soak_current) / soak_current
            return abs(diff) <= 0.04 or (current >= soak_current)
        except RedisError:
            return False

    def current_at_soak(self, event):
        try:
            current = float(redis.read(MAGNET_CURRENT_KEY)[1])
            soak_current = float(redis.read(SOAK_CURRENT_KEY))
            diff = (current - soak_current) / soak_current
            return abs(diff) <= 0.04 or (current >= soak_current)
        except RedisError:
            return False

    def device_ready_for_regulate(self, event):
        try:
            return float(redis.read(DEVICE_TEMP_KEY)[1]) <= float(redis.read(REGULATION_TEMP_KEY))
        except RedisError:
            return False

    def device_regulatable(self, event):
        """
        Return True if the device is at a temperature at which the PID loop can regulate it

        NOTE: enforce_upper_limit is controlled by an ENGINEERING KEY that must be changed DIRECTLY IN REDIS. It cannot
         be commanded and must be manually changed
        """
        enforce_upper_limit = redis.read(IMPOSE_UPPER_LIMIT_ON_REGULATION_KEY)
        if enforce_upper_limit == "on":
            try:
                return float(redis.read(DEVICE_TEMP_KEY)[1]) <= MAX_REGULATE_TEMP
            except RedisError:
                return False
        else:
            return True

    def kill_current(self, event):
        """Kill the current if possible, return False if fail"""
        try:
            ls625.kill_current()
            return True
        except IOError:
            return False

    def record_entry(self, event):
        self.state_entry_time[self.state] = time.time()
        redis.store({MAGNET_STATE_KEY: self.state.replace('_', ' ')})
        write_persisted_state(self.statefile, self.state)


if __name__ == "__main__":
    util.setup_logging('magnetAgent')
    redis.setup_redis(ts_keys=TS_KEYS)
    # MAX_REGULATE_TEMP = 1.50 * float(redis.read(REGULATION_TEMP_KEY))
    MAX_REGULATE_TEMP = np.inf

    try:
        statefile = redis.read(STATEFILE_PATH_KEY)
    except KeyError:
        statefile = pkg_resources.resource_filename('mkidcontrol', '../configuration/magnet.statefile')
        redis.store({STATEFILE_PATH_KEY: statefile})

    controller = MagnetController(statefile=statefile)
    redis.store({IMPOSE_UPPER_LIMIT_ON_REGULATION_KEY: 'off'})

    # main loop, listen for commands and handle them
    try:
        while True:
            for key, val in redis.listen(COMMAND_KEYS):
                log.debug(f"Redis listened to something! Key: {key} -- Val: {val}")
                key = key.removeprefix('command:')
                if key in SETTING_KEYS:
                    try:
                        log.debug(f"Setting {key} -> {val}")
                        cmd = LakeShoreCommand(key, val)
                        redis.store({key: val})
                    except (IOError, StateError):
                        pass
                    except ValueError:
                        log.warning(f"Ignoring invalid command ('{key}={val}'): {e}")
                # NB I'm disinclined to include forced state overrides but they would go here
                elif key == REGULATION_TEMP_KEY:
                    MAX_REGULATE_TEMP = 1.50 * float(redis.read(REGULATION_TEMP_KEY))
                    redis.publish(f"command:{LAKESHORE_SETPOINT_KEY}", val, store=False)
                    redis.store({REGULATION_TEMP_KEY: val}, store=False)
                elif key == ABORT_CMD:
                    # abort any cooldown in progress, warm up, and turn things off
                    # e.g. last command before heading to bed
                    controller.abort()
                elif key == QUENCH_KEY:
                    controller.quench()
                elif key == COLD_AT_CMD:
                    try:
                        controller.schedule_cooldown(datetime.fromtimestamp(float(val)))
                        redis.store({COOLDOWN_SCHEDULED_KEY: 'yes'})
                        redis.store({SCHEDULED_COOLDOWN_TIMESTAMP_KEY: f"{time.timestamp()}"})
                    except ValueError as e:
                        log.error(e)
                elif key == COLD_NOW_CMD:
                    try:
                        controller.start()
                    except MachineError:
                        log.info('Cooldown already in progress', exc_info=True)
                elif key == CANCEL_COOLDOWN_CMD:
                    try:
                        controller.cancel_scheduled_cooldown()
                        redis.store({COOLDOWN_SCHEDULED_KEY: 'no'})
                        redis.store({SCHEDULED_COOLDOWN_TIMESTAMP_KEY: ""})
                    except Exception as e:
                        log.error(e)
                else:
                    log.info(f'Ignoring {key}:{val}')
                redis.store({CONTROLLER_STATUS_KEY: controller.status})

    except RedisError as e:
        log.critical(f"Redis server error! {e}", exc_info=True)
        controller.deramp()

        try:
            while not controller.is_off():
                log.info(f'Waiting (10s) for magnet to deramp from ({redis.read(MAGNET_CURRENT_KEY)[1]}) before exiting...')
                time.sleep(10)
        except IOError:
            pass
        sys.exit(1)
