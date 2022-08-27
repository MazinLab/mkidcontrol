"""
Author: Noah Swimmer
19 July 2022

Program for controlling the magnet. The magnet controller itself is a statemachine but requires no instruments to run.
It will run even with no lakeshore/heatswitch/etc., although it will not allow anything to actually happen.
"""

import sys
import time
import logging
import threading
from datetime import datetime, timedelta
from collections import defaultdict
from transitions import MachineError, State
from transitions.extensions import LockedMachine
import pkg_resources

from mkidcontrol.devices import write_persisted_state, load_persisted_state
from mkidcontrol.mkidredis import RedisError
import mkidcontrol.util as util
import mkidcontrol.mkidredis as redis
from mkidcontrol.commands import LakeShoreCommand, COMMANDSMAGNET
import mkidcontrol.agents.xkid.heatswitchAgent as heatswitch
import mkidcontrol.agents.lakeshore372Agent as ls372
import mkidcontrol.agents.lakeshore625Agent as ls625

QUERY_INTERVAL = 1
MAX_PERSISTED_STATE_LIFE_SECONDS = 3600

SETTING_KEYS = tuple(COMMANDSMAGNET.keys())

SOAK_TIME_KEY = 'device-settings:magnet:soak-time'
SOAK_CURRENT_KEY = 'device-settings:magnet:soak-current'
RAMP_RATE_KEY = 'device-settings:magnet:ramp-rate'
DERAMP_RATE_KEY = 'device-settings:magnet:ramp-rate'
COOLDOWN_SCHEDULED_KEY = 'device-settings:magnet:cooldown-scheduled'

IMPOSE_UPPER_LIMIT_ON_REGULATION_KEY = 'device-settings:magnet:enable-temperature-regulation-upper-limit'
STATEFILE_PATH_KEY = 'device-settings:magnet:statefile'  # /mkidcontrol/mkidcontrol/logs/statefile.txt

STOP_RAMP_KEY = 'command:device-settings:ls625:stop-current-ramp'
COLD_AT_CMD = 'be-cold-at'
COLD_NOW_CMD = 'get-cold'
ABORT_CMD = 'abort-cooldown'
CANCEL_COOLDOWN_CMD = 'cancel-scheduled-cooldown'
QUENCH_KEY = 'event:quenching'

MAGNET_COMMAND_KEYS = (COLD_AT_CMD, COLD_NOW_CMD, ABORT_CMD, CANCEL_COOLDOWN_CMD, STOP_RAMP_KEY)

MAGNET_STATE_KEY = 'status:magnet:state'  # OFF | RAMPING | SOAKING | QUENCH (DON'T QUENCH!)
MAGNET_CURRENT_KEY = 'status:magnet:current'
MAGNET_FIELD_KEY = 'status:magnet:field'
CONTROLLER_STATUS_KEY = 'status:magnet:status'
OUTPUT_VOLTAGE_KEY = 'status:device:ls625:output-voltage'

TS_KEYS = [MAGNET_CURRENT_KEY, MAGNET_FIELD_KEY, OUTPUT_VOLTAGE_KEY]

COMMAND_KEYS = [f"command:{k}" for k in MAGNET_COMMAND_KEYS + SETTING_KEYS]

DEVICE_TEMP_KEY = 'status:temps:device-stage:temp'
REGULATION_TEMP_KEY = "device-settings:device-stage:regulating-temp"

MAGNETKEYS = TS_KEYS + [SOAK_TIME_KEY, SOAK_CURRENT_KEY, COOLDOWN_SCHEDULED_KEY, STOP_RAMP_KEY, RAMP_RATE_KEY]

log = logging.getLogger()


class StateError(Exception):
    pass


def compute_initial_state(statefile):
    # TODO: Check legality and test funtions
    initial_state = 'deramping'  # always safe to start here
    redis.store({COOLDOWN_SCHEDULED_KEY: 'no'})
    try:
        if ls625.is_initialized():
            if ls372.in_pid_output():
                initial_state = 'regulating'  # NB if HS in wrong position (closed) device won't stay cold and we'll transition to deramping
            else:
                state_time, persisted_state = load_persisted_state(statefile)
                if persisted_state is None or time.time()-state_time > MAX_PERSISTED_STATE_LIFE_SECONDS:
                    return initial_state
                else:
                    initial_state = persisted_state
                current = ls625.lakeshore_current()
                if initial_state == 'soaking' and (current >= 0.97 * float(redis.read(SOAK_CURRENT_KEY))) and (current <= 1.03 * float(redis.read(SOAK_CURRENT_KEY))):
                    initial_state = 'ramping'  # we can recover

                # be sure the command is sent
                if initial_state in ('hs_closing',):
                    heatswitch.close()

                if initial_state in ('hs_opening',):
                    heatswitch.open()

                # failure cases
                if ((initial_state in ('ramping', 'soaking') and heatswitch.is_opened()) or
                        (initial_state in ('cooling',) and heatswitch.is_closed()) or
                        (initial_state in ('off', 'regulating'))):
                    initial_state = 'deramping'  # deramp to off, we are out of sync with the hardware

    except IOError:
        log.critical('Lost ls625 connection during agent startup. defaulting to deramping')
        initial_state = 'deramping'
    except RedisError:
        log.critical('Lost redis connection during compute_initial_state startup.')
        raise
    log.info(f"\n\n------ Initial State is: {initial_state} ------\n")
    return initial_state


from wtforms.fields import *
from wtforms.fields.html5 import *
from wtforms.validators import *
from flask_wtf import FlaskForm


class ScheduleForm(FlaskForm):
    at = DateTimeLocalField('Schedule cycle for:', format='%m/%d/%Y %I:%M %p')
    schedule = SubmitField("Schedule")


class MagnetCycleSettingsForm(FlaskForm):
    # TODO: Turn this into something that can be used to either modify the standard/fast cycle OR run a custom cycle
    soak_current = FloatField("Soak Current (A)", default=7.88, validators=[NumberRange(0, 10.0)])
    soak_time = IntegerField("Soak Time (minutes)", default=30, validators=[NumberRange(0, 240)])
    ramp_rate = FloatField("Ramp rate (A/s)", default=0.015, validators=[NumberRange(0, 0.100)])
    deramp_rate = FloatField("Deramp rate (A/s)", default=0.020, validators=[NumberRange(0, 0.100)])
    update = SubmitField("Update")
    start = SubmitField("Start")


class MagnetCycleForm(FlaskForm):
    # TODO: Ramp dropdown (standard ramp/fast ramp/custom ramp?)
    # TODO: make validators a function of the limits? We can just read them in from redis with no issue
    start = SubmitField("Start Standard Cycle")
    fast = SubmitField("Start Fast Cycle")
    custom = SubmitField("Start Custom Ramp")
    abort = SubmitField("Abort Cooldown")
    cancel_scheduled = SubmitField("Cancel Scheduled Cooldown")
    update = SubmitField("Update")


class MagnetController(LockedMachine):

    LOOP_INTERVAL = 1
    BLOCKS = defaultdict(set)  # This holds the ls625 commands that are blocked out in a given state i.e.
                               #  'regulating':('device-settings:ls625:setpoint-mode',)

    def __init__(self, statefile='./magnetstate.txt'):
        transitions = [
            {'trigger': 'abort', 'source': '*', 'dest': 'deramping'},

            {'trigger': 'quench', 'source': '*', 'dest': 'off'},

            {'trigger': 'start', 'source': 'off', 'dest': 'hs_closing',
             'prepare': ('close_heatswitch', 'ls372_to_no_output', 'to_manual_mode')},
            {'trigger': 'start', 'source': 'deramping', 'dest': 'hs_closing',
             'prepare': ('close_heatswitch', 'ls372_to_no_output', 'to_manual_mode')},

            {'trigger': 'next', 'source': 'hs_closing', 'dest': None,
             'prepare': 'close_heatswitch'},
            {'trigger': 'next', 'source': 'hs_closing', 'dest': 'starting_ramp',
             'conditions': ('heatswitch_closed', 'ls372_in_no_output')},

            {'trigger': 'next', 'source': 'starting_ramp', 'dest': 'ramping',
             'conditions': ('heatswitch_closed', 'in_manual_mode'), 'prepare': 'start_current_ramp'},

            {'trigger': 'next', 'source': 'ramping', 'dest': None, 'conditions': 'ramp_ok',
             'unless': 'current_ready_to_soak'},
            {'trigger': 'next', 'source': 'ramping', 'dest': 'soaking', 'conditions': 'current_ready_to_soak'},
            {'trigger': 'next', 'source': 'ramping', 'dest': 'deramping'},

            {'trigger': 'next', 'source': 'soaking', 'dest': None, 'unless': 'soak_time_expired',
             'conditions': 'current_at_soak'},
            {'trigger': 'next', 'source': 'soaking', 'dest': 'starting_deramp', 'prepare': ('open_heatswitch',),
             'conditions': ('current_at_soak', 'soak_time_expired')},
            {'trigger': 'next', 'source': 'soaking', 'dest': 'deramping'},

            {'trigger': 'next', 'source': 'starting_deramp', 'dest': 'cooling', 'conditions': 'heatswitch_opened',
             'prepare': 'start_current_deramp'},

            {'trigger': 'next', 'source': 'cooling', 'dest': None, 'unless': 'device_ready_for_regulate',
             'conditions': ('heatswitch_opened', 'deramp_ok')},
            {'trigger': 'next', 'source': 'cooling', 'dest': 'prep_regulating',
             'conditions': ('heatswitch_opened', 'device_ready_for_regulate')},
            {'trigger': 'next', 'source': 'cooling', 'dest': 'deramping', 'conditions': 'heatswitch_closed'},

            {'trigger': 'next', 'source': 'prep_regulating', 'dest': None, 'prepare': 'to_pid_mode',
             'conditions': ('heatswitch_opened', 'device_ready_for_regulate'), 'unless': 'in_pid_mode'},
            {'trigger': 'next', 'source': 'prep_regulating', 'dest': 'regulating',
             'conditions': ('heatswitch_opened', 'device_ready_for_regulate', 'in_pid_mode'), 'after': 'ls372_to_pid'},
            {'trigger': 'next', 'source': 'prep_regulating', 'dest': 'deramping'},

            {'trigger': 'next', 'source': 'regulating', 'dest': None,
             'conditions': ('device_regulatable', 'in_pid_mode', 'ls372_in_pid')},
            {'trigger': 'next', 'source': 'regulating', 'dest': None,
             'conditions': ('device_regulatable', 'in_pid_mode', 'ls372_in_no_output'), 'prepare': 'ls372_to_pid'},
            {'trigger': 'next', 'source': 'regulating', 'dest': 'deramping'},

            {'trigger': 'next', 'source': 'deramping', 'dest': None, 'prepare': ('to_manual_mode', 'kill_current'),
             'unless': 'current_off'},
            {'trigger': 'next', 'source': 'deramping', 'dest': 'off', 'prepare': ('ls372_to_no_output', 'kill_current')},

            {'trigger': 'next', 'source': 'off', 'dest': None}
        ]

        states = (  # Entering off MUST succeed
            State('off', on_enter=['record_entry', 'kill_current']),
            State('hs_closing', on_enter='record_entry'),
            State('starting_ramp', on_enter='record_entry'),
            State('ramping', on_enter='record_entry'),
            State('soaking', on_enter='record_entry'),
            State('hs_opening', on_enter='record_entry'),
            State('starting_deramp', on_enter='record_entry'),
            State('cooling', on_enter='record_entry'),
            State('prep_regulating', on_enter='record_entry'),
            State('regulating', on_enter='record_entry'),
            # Entering ramping MUST succeed
            State('deramping', on_enter='record_entry'))

        self.statefile = statefile
        self.lock = threading.RLock()
        self.scheduled_cooldown = None
        self._run = False  # Set to false to kill the main loop
        self._mainthread = None

        initial = compute_initial_state(self.lakeshore, self.statefile)
        self.state_entry_time = {initial: time.time()}
        LockedMachine.__init__(self, transitions=transitions, initial=initial, states=states, machine_context=self.lock,
                               send_event=True)

        self.start_main()

    def initialize_lakeshore(self):
        """
        Callback run on connection to the lakeshore whenever it is not initialized. This will only happen if the sim loses all
        of its settings, which should never every happen. Any settings applied take immediate effect
        """
        self.firmware_pull()
        try:
            self.set_redis_settings(init_blocked=True) # If called the sim is in a blank state and needs everything!
        except (RedisError, KeyError) as e:
            raise IOError(e)  # we can't initialize!


    def start_main(self):
        self._run = True  # Set to false to kill the main loop
        self._mainthread = threading.Thread(target=self._main)
        self._mainthread.daemon = True
        self._mainthread.start()

    def _main(self):
        while self._run:
            try:
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
        soak_time = float(redis.read(SOAK_TIME_KEY))
        ramp_rate = float(redis.read(RAMP_RATE_KEY))
        deramp_rate = -1 * float(redis.read(RAMP_RATE_KEY))
        current_current = self.lakeshore.current()
        current_state = self.state # NB: If current_state is regulating time_to_cool will return 0 since it is already cool.

        time_to_cool = 0
        if current_state in ('ramping', 'off', 'hs_closing'):
            time_to_cool = ((soak_current - current_current) / ramp_rate) + soak_time + ((0 - soak_current) / deramp_rate)
        if current_state in ('soaking', 'hs_opening'):
            time_to_cool = (time.time() - self.state_entry_time['soaking']) + ((0 - soak_current) / deramp_rate)
        if current_state in ('cooling', 'deramping'):
            time_to_cool = -1 * current_current / deramp_rate

        return timedelta(seconds=time_to_cool)

    def schedule_cooldown(self, time):
        """time specifies the time by which to be cold"""
        if self.state not in ('off', 'deramping'):
            raise ValueError(f'Cooldown in progress, abort before scheduling.')

        now = datetime.now()
        time_needed = self.min_time_until_cool

        if time < now + time_needed:
            raise ValueError(f'Time travel not possible, specify a time at least {time_needed} in the future. (Current time: {now.timestamp()})')

        self.cancel_scheduled_cooldown()
        redis.store({COOLDOWN_SCHEDULED_KEY: 'no'})
        t = threading.Timer((time - time_needed - now).seconds, self.start) # TODO (For JB): self.start?
        self.scheduled_cooldown = (time - time_needed, t)
        redis.store({COOLDOWN_SCHEDULED_KEY: 'yes'})
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

    def current_off(self, event):
        try:
            return redis.read('device-settings:ls625:control-mode') == "Internal" and \
                   float(redis.read('device-settings:ls625:desired-current')) == 0.0 and \
                   abs(float(redis.read(MAGNET_CURRENT_KEY)[1])) <= 0.003
        except RedisError:
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

    def soak_time_expired(self, event):
        try:
            return (time.time() - self.state_entry_time['soaking']) >= float(redis.read(SOAK_TIME_KEY))
        except RedisError:
            return False

    def current_ready_to_soak(self, event):
        # Test if the current is within 3% of the soak value, typically values are WELL within this limit
        try:
            current = float(redis.read(MAGNET_CURRENT_KEY)[1])
            soak_current = float(redis.read(SOAK_CURRENT_KEY))
            diff = (current - soak_current) / soak_current
            return diff <= 0.03
        except RedisError:
            return False

    def current_at_soak(self, event):
        # Test if the current is within 3% of the soak value, typically values are WELL within this limit
        try:
            current = float(redis.read(MAGNET_CURRENT_KEY)[1])
            soak_current = float(redis.read(SOAK_CURRENT_KEY))
            diff = (current - soak_current) / soak_current
            return diff <= 0.03
        except RedisError:
            return False

    def in_pid_mode(self, event):
        try:
            return ls625.in_pid_mode()
        except RedisError:
            return False

    def to_pid_mode(self, event):
        try:
            ls625.to_pid_mode()
        except RedisError:
            return False

    def in_manual_mode(self, event):
        try:
            return ls625.in_manual_mode()
        except RedisError:
            return False

    def to_manual_mode(self, event):
        try:
            ls625.to_manual_mode()
        except RedisError:
            return False

    def start_current_ramp(self, event):
        try:
            ls625.start_cycle_ramp()
        except RedisError:
            return False

    def start_current_deramp(self, event):
        try:
            ls625.start_cycle_deramp()
        except RedisError:
            return False

    def ramp_ok(self, event):
        # TODO

        # if self.state == 'ramping' and self.lakeshore.last_current_read >= 0 and self.lakeshore.last_current_read <= redis.read(SOAK_CURRENT_KEY):
        if self.state == 'ramping':
            return True
        else:
            return False

    def deramp_ok(self, event):
        # TODO
        # if self.state == 'deramping' and self.lakeshore.last_current_read >= 0 and self.lakeshore.last_current_read <= redis.read(SOAK_CURRENT_KEY):
        if self.state == 'deramping':
            return True
        else:
            return False

    def device_ready_for_regulate(self, event):
        try:
            return float(redis.read(DEVICE_TEMP_KEY)[1]) <= float(redis.read(REGULATION_TEMP_KEY)) * 1.5
        except RedisError:
            return False

    def device_regulatable(self, event):
        """
        Return True if the device is at a temperature at which the PID loop can regulate it

        NOTE: enforce_upper_limit is controlled by an ENGINEERING KEY that must be changed DIRECTLY IN REDIS. It cannot
         be commanded and must be manually changed

        TODO: Consider adding more logic into this to see if the current is still able to provide regulation?
        """
        enforce_upper_limit = redis.read(IMPOSE_UPPER_LIMIT_ON_REGULATION_KEY)
        try:
            if enforce_upper_limit == "on":
                return float(redis.read(DEVICE_TEMP_KEY)[1]) <= MAX_REGULATE_TEMP
            else:
                return True
        except RedisError:
            return False

    def kill_current(self, event):
        """Kill the current if possible, return False if fail"""
        try:
            ls625.kill_current()
            return True
        except IOError:
            return False

    def record_entry(self, event):
        self.state_entry_time[self.state] = time.time()
        log.info(f"Recorded entry: {self.state}")
        redis.store({MAGNET_STATE_KEY: self.state})
        write_persisted_state(self.statefile, self.state)


if __name__ == "__main__":
    util.setup_logging('lakeshore625Agent')
    redis.setup_redis(ts_keys=TS_KEYS)
    MAX_REGULATE_TEMP = 1.50 * float(redis.read(REGULATION_TEMP_KEY))

    try:
        statefile = redis.read(STATEFILE_PATH_KEY)
    except KeyError:
        statefile = pkg_resources.resource_filename('mkidcontrol', '../configuration/magnet.statefile')
        redis.store({STATEFILE_PATH_KEY: statefile})

    controller = MagnetController(statefile=statefile)
    redis.store({IMPOSE_UPPER_LIMIT_ON_REGULATION_KEY: 'on'})

    # main loop, listen for commands and handle them
    try:
        while True:
            for key, val in redis.listen(COMMAND_KEYS):
                log.debug(f"Redis listened to something! Key: {key} -- Val: {val}")
                key = key.removeprefix('command:')
                if key in SETTING_KEYS:
                    try:
                        cmd = LakeShoreCommand(key, val)
                        controller.ls_command(cmd)
                        redis.store({cmd.setting: cmd.value})
                    except (IOError, StateError):
                        pass
                    except ValueError:
                        log.warning(f"Ignoring invalid command ('{key}={val}'): {e}")
                # NB I'm disinclined to include forced state overrides but they would go here
                elif key == REGULATION_TEMP_KEY:
                    MAX_REGULATE_TEMP = 1.50 * float(redis.read(REGULATION_TEMP_KEY))
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
                    except:
                        # Add error handling here
                        pass
                else:
                    log.info(f'Ignoring {key}:{val}')
                redis.store({CONTROLLER_STATUS_KEY: controller.status})

    except RedisError as e:
        log.critical(f"Redis server error! {e}", exc_info=True)
        # TODO insert something to suppress the concomitant redis monitor thread errors that will spam logs?
        controller.deramp()

        try:
            while not controller.is_off():
                log.info(f'Waiting (10s) for magnet to deramp from ({controller.sim.setpoint()}) before exiting...')
                time.sleep(10)
        except IOError:
            pass
        sys.exit(1)
