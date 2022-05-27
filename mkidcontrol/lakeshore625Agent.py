"""
Author: Noah Swimmer
21 February 2022

Program for communicating with and controlling the LakeShore625 Superconducting Magnet Power Supply.

This module is responsible for TODO

NOTE: At powerup, the current is set initially to 0.00 A

TODO: Quench detection values. Theory for  value choice exists at npage 48 of the LakeShore 625 manual
 (V_LS625compliance,max = 5V, V_max,magnet=125 mV,  L=~35H, I_max=9.4A)
"""

import sys
import time
import logging
import threading
from datetime import datetime, timedelta
from collections import defaultdict
from transitions import MachineError, State, Transition
from transitions.extensions import LockedMachine
import pkg_resources

from mkidcontrol.devices import LakeShore625, MagnetState, write_persisted_state, load_persisted_state
from mkidcontrol.mkidredis import RedisError
import mkidcontrol.util as util
import mkidcontrol.mkidredis as redis
from mkidcontrol.commands import COMMANDS625, LakeShoreCommand
import mkidcontrol.heatswitchAgent as heatswitch
import mkidcontrol.lakeshore372Agent as ls372

QUERY_INTERVAL = 1
MAX_PERSISTED_STATE_LIFE_SECONDS = 3600

DEVICE = '/dev/ls625'
VALID_MODELS = ('MODEL625', )

SOAK_TIME_KEY = 'device-settings:ls625:soak-time'
SOAK_CURRENT_KEY = 'device-settings:ls625:soak-current'
RAMP_RATE_KEY = 'device-settings:ls625:ramp-rate'
COOLDOWN_SCHEDULED_KEY = 'device-settings:ls625:cooldown-scheduled'

DESIRED_CURRENT_KEY = 'device-settings:ls625:desired-current'

IMPOSE_UPPER_LIMIT_ON_REGULATION_KEY = 'device-settings:ls625:enable-temperature-regulation-upper-limit'
STATEFILE_PATH_KEY = 'device-settings:ls625:statefile'  # /mkidcontrol/mkidcontrol/logs/statefile.txt

STOP_RAMP_KEY = 'command:device-settings:ls625:stop-current-ramp'
COLD_AT_CMD = 'be-cold-at'
COLD_NOW_CMD = 'get-cold'
ABORT_CMD = 'abort-cooldown'
CANCEL_COOLDOWN_CMD = 'cancel-scheduled-cooldown'
QUENCH_KEY = 'event:quenching'

MAGNET_COMMAND_KEYS = (COLD_AT_CMD, COLD_NOW_CMD, ABORT_CMD, CANCEL_COOLDOWN_CMD, STOP_RAMP_KEY, DESIRED_CURRENT_KEY)

SETTING_KEYS = tuple(COMMANDS625.keys())

STATUS_KEY = "status:device:ls625:status"
FIRMWARE_KEY = "status:device:ls625:firmware"
MODEL_KEY = 'status:device:ls625:model'
SN_KEY = 'status:device:ls625:sn'

MAGNET_STATE_KEY = 'status:magnet:state' # OFF | RAMPING | SOAKING | QUENCH (DON'T QUENCH!)
MAGNET_CURRENT_KEY = 'status:magnet:current'
MAGNET_FIELD_KEY = 'status:magnet:field'
OUTPUT_VOLTAGE_KEY = 'status:device:ls625:output-voltage'

TS_KEYS = [MAGNET_CURRENT_KEY, MAGNET_FIELD_KEY, OUTPUT_VOLTAGE_KEY]

COMMAND_KEYS = [f"command:{k}" for k in SETTING_KEYS + MAGNET_COMMAND_KEYS]

DEVICE_TEMP_KEY = 'status:temps:device-stage:temp'
REGULATION_TEMP_KEY = "device-settings:device-stage:regulating-temp"

LS625KEYS = TS_KEYS + [STATUS_KEY, FIRMWARE_KEY, MODEL_KEY, SN_KEY, SOAK_TIME_KEY, SOAK_CURRENT_KEY,
                       COOLDOWN_SCHEDULED_KEY, STOP_RAMP_KEY, RAMP_RATE_KEY]

log = logging.getLogger()


class StateError(Exception):
    pass


def monitor_callback(cur, field, ov):
    d = {k: x for k, x in zip((MAGNET_CURRENT_KEY, MAGNET_FIELD_KEY, OUTPUT_VOLTAGE_KEY), (cur, field, ov)) if x}
    try:
        redis.store(d, timeseries=True)
    except RedisError:
        log.warning('Storing magnet status to redis failed')


def compute_initial_state(lakeshore, statefile):
    # TODO: Check all logic here
    initial_state = 'deramping'  # always safe to start here
    redis.store({COOLDOWN_SCHEDULED_KEY: 'no'})
    try:
        if lakeshore.initialized_at_last_connect:
            mag_state = lakeshore.mode
            if mag_state == MagnetState.PID:
                initial_state = 'regulating'  # NB if HS in wrong position (closed) device won't stay cold and we'll transition to deramping
            else:
                state_time, persisted_state = load_persisted_state(statefile)
                if persisted_state is None or time.time()-state_time > MAX_PERSISTED_STATE_LIFE_SECONDS:
                    return initial_state
                else:
                    initial_state = persisted_state
                current = lakeshore.current
                if initial_state == 'soaking' and (current >= 0.98 * float(redis.read(SOAK_CURRENT_KEY))) and (current <= 1.02 * float(redis.read(SOAK_CURRENT_KEY))):
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


class MagnetController(LockedMachine):
    LOOP_INTERVAL = 1
    BLOCKS = defaultdict(set)  # This holds the ls625 commands that are blocked out in a given state i.e.
                               #  'regulating':('device-settings:ls625:setpoint-mode',)

    def __init__(self, statefile='./magnetstate.txt'):
        # TODO: Make sure everything here tracks, especially when entering regulation! Make sure the order of operation
        #  is correct for switching the LS372 to PID mode and LS625 to externally programmed current.
        transitions = [
            {'trigger': 'abort', 'source': '*', 'dest': 'deramping'},

            {'trigger': 'quench', 'source': '*', 'dest': 'off'},

            {'trigger': 'start', 'source': 'off', 'dest': 'hs_closing',
             'prepare': ('close_heatswitch', 'ls372_to_no_output')},
            {'trigger': 'start', 'source': 'deramping', 'dest': 'hs_closing',
             'prepare': ('close_heatswitch', 'ls372_to_no_output')},

            {'trigger': 'next', 'source': 'hs_closing', 'dest': 'starting_ramp', 'conditions': 'heatswitch_closed'},
            {'trigger': 'next', 'source': 'hs_closing', 'dest': None,
             'prepare': ('close_heatswitch', 'ls372_to_no_output')},

            {'trigger': 'next', 'source': 'starting_ramp', 'dest': None, 'conditions': 'heatswitch_closed',
             'after': 'start_current_ramp'},
            {'trigger': 'next', 'source': 'starting_ramp', 'dest': 'ramping',
             'conditions': ('heatswitch_closed', 'ramp_ok')},

            {'trigger': 'next', 'source': 'ramping', 'dest': None, 'conditions': 'ramp_ok',
             'unless': 'current_ready_to_soak'},
            {'trigger': 'next', 'source': 'ramping', 'dest': 'soaking', 'conditions': 'current_ready_to_soak'},

            {'trigger': 'next', 'source': 'soaking', 'dest': None, 'unless': 'soak_time_expired',
             'conditions': 'current_at_soak'},
            {'trigger': 'next', 'source': 'soaking', 'dest': 'hs_opening', 'prepare': ('open_heatswitch',),
             'conditions': ('current_at_soak', 'soak_time_expired')},
            {'trigger': 'next', 'source': 'soaking', 'dest': 'deramping'},

            {'trigger': 'next', 'source': 'hs_opening', 'dest': 'starting_deramp',
             'conditions': ('heatswitch_opened',)},
            {'trigger': 'next', 'source': 'hs_opening', 'dest': None, 'prepare': ('open_heatswitch',)},

            {'trigger': 'next', 'source': 'starting_deramp', 'dest': None, 'conditions': 'heatswitch_opened',
             'after': 'start_current_deramp'},
            {'trigger': 'next', 'source': 'starting_deramp', 'dest': 'cooling',
             'conditions': ('heatswitch_opened', 'deramp_ok')},

            {'trigger': 'next', 'source': 'cooling', 'dest': None, 'unless': 'device_ready_for_regulate',
             'conditions': ('heatswitch_opened', 'deramp_ok')},
            {'trigger': 'next', 'source': 'cooling', 'dest': 'regulating', 'before': 'to_pid_mode',
             'conditions': ('heatswitch_opened',), 'prepare': ('ls372_to_pid_mode',)},
            {'trigger': 'next', 'source': 'cooling', 'dest': 'deramping', 'conditions': 'heatswitch_closed'},

            {'trigger': 'next', 'source': 'regulating', 'dest': None,
             'conditions': ['device_regulatable', 'in_pid_mode']},
            {'trigger': 'next', 'source': 'regulating', 'dest': 'deramping'},

            {'trigger': 'next', 'source': 'deramping', 'dest': None, 'unless': 'current_off'},
            {'trigger': 'next', 'source': 'deramping', 'dest': 'off', 'prepare': 'ls372_to_no_output'},

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
            State('regulating', on_enter='record_entry'),
            # Entering ramping MUST succeed
            State('deramping', on_enter='record_entry'))

        lakeshore = LakeShore625(name='ls625', port=DEVICE, valid_models=VALID_MODELS, initializer=self.initialize_lakeshore)
        # NB If the settings are manufacturer defaults then the ls625 had a major upset, generally initialize_sim
        # will not be called

        # Kick off a thread to run forever and just log data into redis
        lakeshore.monitor(QUERY_INTERVAL, (lakeshore.current, lakeshore.field, lakeshore.output_voltage),
                          value_callback=monitor_callback)

        self.statefile = statefile
        self.lakeshore = lakeshore
        self.lock = threading.RLock()
        self.scheduled_cooldown = None
        self._run = False  # Set to false to kill the main loop
        self._mainthread = None

        initial = compute_initial_state(self.sim, self.statefile)
        self.state_entry_time = {initial: time.time()}
        LockedMachine.__init__(self, transitions=transitions, initial=initial, states=states, machine_context=self.lock,
                               send_event=True)

        if lakeshore.initialized_at_last_connect:
            self.firmware_pull()
            self.set_redis_settings(init_blocked=False)  #allow IO and Redis errors to shut things down.

        self.start_main()

    def initialize_lakeshore(self):
        """
        Callback run on connection to the sim whenever it is not initialized. This will only happen if the sim loses all
        of its settings, which should never every happen. Any settings applied take immediate effect
        """
        self.firmware_pull()
        try:
            self.set_redis_settings(init_blocked=True) # If called the sim is in a blank state and needs everything!
        except (RedisError, KeyError) as e:
            raise IOError(e)  # we can't initialize!

    def firmware_pull(self):
        # Grab and store device info
        try:
            info = self.lakeshore.device_info
            d = {FIRMWARE_KEY: info['firmware'], MODEL_KEY: info['model'], SN_KEY: info['sn']}
        except IOError as e:
            log.error(f"When checking device info: {e}")
            d = {FIRMWARE_KEY: '', MODEL_KEY: '', SN_KEY: ''}
        try:
            redis.store(d)
        except RedisError:
            log.warning('Storing device info to redis failed')

    def set_redis_settings(self, init_blocked=False):
        """may raise IOError, if so sim can be in a partially configured state"""
        try:
            settings_to_load = redis.read(SETTING_KEYS, error_missing=True)
        except RedisError:
            log.critical('Unable to pull settings from redis to initialize ls625')
            raise
        except KeyError as e:
            log.critical('Unable to pull setting {e} from redis to initialize ls625')
            raise

        blocks = self.BLOCKS[self.state]
        blocked_init = blocks.intersection(settings_to_load.keys())

        current_settings = {}
        if blocked_init:
            if init_blocked:
                # TODO: Error below? (11 March 2020: Not sure what error we want here, N.S.)
                for_logging = "\n\t".join(blocked_init)
                log.warning(f'Initializing \n\t{for_logging}\n despite being blocked by current state.')
            else:
                for_logging = "\n\t".join(blocked_init)
                log.warning(f'Skipping settings \n\t{for_logging}\n as they are blocked by current state.')
                settings_to_load = {k: v for k, v in settings_to_load if k not in blocks}
                current_settings = self.lakeshore.read_schema_settings(blocked_init)  #keep redis in sync

        initialized_settings = self.lakeshore.apply_schema_settings(settings_to_load)
        initialized_settings.update(current_settings)
        try:
            redis.store(initialized_settings)
        except RedisError:
            log.warning('Storing device settings to redis failed')

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
            return self.lakeshore.mode == MagnetState.MANUAL and self.current() <= 0.002
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
        try:
            return self.lakeshore.current() >= float(redis.read(SOAK_CURRENT_KEY))
        except RedisError:
            return False

    def current_at_soak(self, event):
        try:
            return self.lakeshore.current() >= .98 * float(redis.read(SOAK_CURRENT_KEY))
        except RedisError:
            return False

    def in_pid_mode(self, event):
        return self.lakeshore.mode == MagnetState.PID

    def to_pid_mode(self, event):
        self.lakeshore.mode = MagnetState.PID

    def start_current_ramp(self, event):
        # TODO: Consider if the best way to do this is send a command to the lakeshore rather
        #  than a setter. Mostly to make sure that the redis DB stays in sync with everything
        limit = self.lakeshore.MAX_CURRENT
        try:
            desired_current = redis.read(SOAK_CURRENT_KEY)
        except RedisError:
            log.warning(f"Unable to pull {SOAK_CURRENT_KEY}, using {limit} A")
            desired_current = limit

        if desired_current > limit:
            log.info(f"Desired soak current too high, overwriting")
            try:
                redis.store({SOAK_CURRENT_KEY: limit})
            except RedisError:
                log.info("Overwriting failed")

        if not desired_current:
            log.warning("No current requested, there cannot be a ramp with no set current")

        try:
            self.lakeshore.set_desired_current(desired_current)
            redis.store({'device-settings:ls625:desired-current': desired_current})
        except IOError:
            log.warning('Failed to start ramp, lakeshore 625 is offline')

    def start_current_deramp(self, event):
        # TODO: Consider if the best way to do this is send a command to the lakeshore rather
        #  than a setter. Mostly to make sure that the redis DB stays in sync with everything
        try:
            self.lakeshore.set_desired_current(0)
            redis.store({'device-settings:ls625:desired-current': 0})
        except IOError:
            log.warning('Failed to start deramp, lakeshore 625 is offline')

    def ramp_ok(self, event):
        # TODO
        return True

    def deramp_ok(self, event):
        # TODO
        return True

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
                return float(redis.read(DEVICE_TEMP_KEY)[1]) <= 1.10 * redis.read(REGULATION_TEMP_KEY)
            except RedisError:
                return False
        else:
            return True

    def kill_current(self, event):
        """Kill the current if possible, return False if fail"""
        try:
            self.lakeshore.kill_current()
            return True
        except IOError:
            return False

    def ls_command(self, cmd):
        """ Directly execute a SimCommand if if possible. May raise IOError or StateError"""
        with self.lock:
            if cmd.setting in self.BLOCKS.get(self.state, tuple()):
                msg = f'Command {cmd} not supported while in state {self.state}'
                log.error(msg)
                raise StateError(msg)
            self.lakeshore.send(cmd.ls_string)

    def record_entry(self, event):
        self.state_entry_time[self.state] = time.time()
        log.info(f"Recorded entry: {self.state}")
        redis.store({MAGNET_STATE_KEY: self.state})
        write_persisted_state(self.statefile, self.state)


if __name__ == "__main__":
    util.setup_logging('lakeshore625Agent')
    redis.setup_redis(create_ts_keys=TS_KEYS)
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
                        controller.sim_command(cmd)
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
                redis.store({STATUS_KEY: controller.status})

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