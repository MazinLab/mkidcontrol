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

MAGNET_COMMAND_KEYS = (COLD_AT_CMD, COLD_NOW_CMD, ABORT_CMD, CANCEL_COOLDOWN_CMD, STOP_RAMP_KEY)

MAGNET_STATE_KEY = 'status:magnet:state'  # Names from statemachine
MAGNET_CURRENT_KEY = 'status:magnet:current'
MAGNET_FIELD_KEY = 'status:magnet:field'
CONTROLLER_STATUS_KEY = 'status:magnet:status'

TS_KEYS = (MAGNET_CURRENT_KEY, MAGNET_FIELD_KEY)

COMMAND_KEYS = [f"command:{k}" for k in MAGNET_COMMAND_KEYS + SETTING_KEYS]

DEVICE_TEMP_KEY = 'status:temps:device-stage:temp'
REGULATION_TEMP_KEY = "device-settings:magnet:regulating-temp"
LAKESHORE_SETPOINT_KEY = 'device-settings:ls372:heater-channel-0:setpoint'

log = logging.getLogger("magentAgent")

ABORT_COOLDOWN = False


class StateError(Exception):
    pass


def compute_initial_state(statefile):
    initial_state = 'deramping'  # always safe to start here
    redis.store({COOLDOWN_SCHEDULED_KEY: 'no'})
    redis.store({SCHEDULED_COOLDOWN_TIMESTAMP_KEY: ''})
    try:
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


class MagnetController:
    LOOP_INTERVAL = 1
    MAX_CURRENT = 9.44  # Amps

    def __init__(self, statefile='./magnetstate.txt'):
        self.lock = threading.RLock()
        self.scheduled_cooldown = None

    @property
    def min_time_until_cool(self):
        """
        return an estimate of the time to cool from the current state
        """
        soak_current = float(redis.read(SOAK_CURRENT_KEY))
        soak_time = float(redis.read(SOAK_TIME_KEY)) * 60  # Soak time stored in minues, must be in seconds
        ramp_rate = float(redis.read(RAMP_RATE_KEY))
        deramp_rate = -1 * float(redis.read(DERAMP_RATE_KEY))  # Deramp rate is stored as a POSITIVE number
        current_current = ls625.lakeshore_current()

        time_to_cool = ((soak_current - current_current) / ramp_rate) + soak_time + (
            (0 - soak_current) / deramp_rate)

        return timedelta(seconds=time_to_cool)

    def schedule_cooldown(self, time):
        """time specifies the time by which to be cold"""
        # TODO how to handle scheduling when we are warming up or other such
        # if self.state not in ('off', 'deramping'):
        #     raise ValueError(f'Cooldown in progress, abort before scheduling.')

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
            
    def _run_cooldown(self, start_time=None, soak_current=9.25, ramp_rate=0.005, deramp_rate=0.005, soak_time=60.0):
        if start_time is None:
            self.start_time = datetime.now()
        else:
            self.start_time = start_time
        self.soak_current = soak_current
        self.ramp_rate = ramp_rate
        self.deramp_rate = deramp_rate

        self.soak_time = soak_time
        self.ramp_time = soak_current / ramp_rate / 60

        self.start_deramp_time = start_time + timedelta(minutes=self.soak_time + self.ramp_time)

        log.info(f'Close heatswitch at {self.start_time.timestamp()}')
        while not ABORT_COOLDOWN:
            if datetime.now() >= self.start_time:
                log.info('Closing heat switch.')
                heatswitch.close()
                break
            time.sleep(1)

        log.info(f'Start magnet cycle at {self.start_time.timestamp()}')
        while not ABORT_COOLDOWN:
            if datetime.now() >= self.start_time:
                log.info('Starting magnet cycle!')
                ls625.start_ramp_up(self.soak_current)
                break
            time.sleep(1)

        log.info(f'start ramp down at {self.start_deramp_time.timestamp()}')
        while not ABORT_COOLDOWN:
            if datetime.now() >= self.start_deramp_time:
                heatswitch.open()
                time.sleep(2)
                if heatswitch.is_opened():
                    log.info('Heat switch open! Current ramping down.')
                    ls625.start_ramp_down(0)
                    ls372.to_pid_output()
                break
            time.sleep(1)

        if ABORT_COOLDOWN:
            ls625.start_ramp_down(0)
            heatswitch.close()

    def start(self):
        soak_current = float(redis.read(SOAK_CURRENT_KEY))
        soak_time = float(redis.read(SOAK_TIME_KEY))
        ramp_rate = float(redis.read(RAMP_RATE_KEY))
        deramp_rate = -1 * float(redis.read(DERAMP_RATE_KEY))
        self._runthread = threading.Thread(target=self._run_cooldown,
                                          kwargs={'soak_current': soak_current,
                                                  'ramp_rate': ramp_rate,
                                                  'deramp_rate': deramp_rate,
                                                  'soak_time': soak_time})
        self._runthread.daemon = True
        self._runthread.start()

    def close_heatswitch(self):
        try:
            heatswitch.close()
        except RedisError:
            pass

    def open_heatswitch(self):
        try:
            heatswitch.open()
        except RedisError:
            pass

    def is_off(self):
        current = ls625.lakeshore_current()
        if current <= 0.010:
            return True
        else:
            return False

    def deramp(self):
        ls625.start_ramp_down(0)

    def abort(self):
        global ABORT_COOLDOWN
        ABORT_COOLDOWN = True
        self._runthread.join()

    def quench(self):
        global ABORT_COOLDOWN
        ABORT_COOLDOWN = True
        ls625.kill_current()


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
