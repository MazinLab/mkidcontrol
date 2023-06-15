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
Command syntax exists at (lower level comms): https://www.zaber.com/documents/ZaberT-SeriesProductsUsersManual2.xx.pdfa

TODO: Use async movement calls for heatswitch?

TODO: This program can behave strangely if the heatswitch motor was moved while the program was running AND CAN ACT IN
 SUCH A WAY THAT IT MAY CAUSE PHYSICAL DAMAGE TO THE HEAT SWITCH MECHANISM. More checks should be made on 'actual' vs 'last recorded' position
"""

import sys
import logging

from mkidcontrol.mkidredis import RedisError
import mkidcontrol.mkidredis as redis
import mkidcontrol.util as util
from mkidcontrol.devices import HeatswitchMotor, HeatswitchPosition
from mkidcontrol.commands import COMMANDSHS, LakeShoreCommand
from zaber_motion import Library

QUERY_INTERVAL = 1

log = logging.getLogger("heatswitchAgent")

SETTING_KEYS = tuple(COMMANDSHS.keys())

STATUS_KEY = 'status:device:heatswitch:status'  # OK | ERROR | OFF
HEATSWITCH_POSITION_KEY = "status:device:heatswitch:position"  # opened | opening | closed | closing
MOTOR_POS = "status:device:heatswitch:motor-position"  # Integer between 0 and 4194303

HEATSWITCH_MOVE_KEY = "device-settings:heatswitch:position"
VELOCITY_KEY = "device-settings:heatswitch:max-velocity"
RUNNING_CURRENT_KEY = "device-settings:heatswitch:running-current"
ACCELERATION_KEY = "device-settings:heatswitch:acceleration"

STOP_KEY = "heatswitch:stop"

COMMAND_KEYS = [f"command:{k}" for k in SETTING_KEYS + tuple(STOP_KEY)]
TS_KEYS = (MOTOR_POS,)


def close():
    redis.publish(f"command:{HEATSWITCH_MOVE_KEY}", HeatswitchPosition.CLOSE, store=False)


def open():
    redis.publish(f"command:{HEATSWITCH_MOVE_KEY}", HeatswitchPosition.OPEN, store=False)


def is_opened():
    # TODO: Check if this will return 'True' fast enough
    return True
    # return not (redis.read(HEATSWITCH_POSITION_KEY) == HeatswitchPosition.CLOSED)


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

        if position == heatswitch.FULL_CLOSE_POSITION:
            initial_state = HeatswitchPosition.CLOSED
        elif position == heatswitch.FULL_OPEN_POSITION:
            initial_state = HeatswitchPosition.OPENED
        else:
            if redis.read(HEATSWITCH_POSITION_KEY) == HeatswitchPosition.OPENING:
                initial_state = HeatswitchPosition.OPENING
            else:
                initial_state = HeatswitchPosition.CLOSING
    except IOError:
        log.critical('Lost heatswitch connection during agent startup. defaulting to unknown')
        initial_state = "unknown"
    except RedisError:
        log.critical('Lost redis connection during compute_initial_state startup.')
        raise
    log.info(f"\n\n------ Initial State is: {initial_state} ------\n")
    return initial_state


if __name__ == "__main__":

    redis.setup_redis(ts_keys=TS_KEYS)
    util.setup_logging('heatswitchAgent')

    try:
        # hs = HeatswitchMotor('/dev/heatswitch', redis, open_position=int((1/2) * 4194303))
        hs = HeatswitchMotor('/dev/heatswitch', redis, open_position=0)
        redis.store({STATUS_KEY: "OK"})
    except RedisError as e:
        log.error(f"Redis server error! {e}")
        sys.exit(1)
    except Exception as e:
        log.critical(f"Could not connect to the heatswitch! Error {e}")
        redis.store({STATUS_KEY: f"Error: {e}"})
        sys.exit(1)

    hs.monitor(QUERY_INTERVAL, (hs.motor_position, hs.state), value_callback=monitor_callback)

    try:
        while True:
            for key, val in redis.listen(COMMAND_KEYS):

                log.debug(f"HeatswitchAgent received {key}, {val}.")
                key = key.removeprefix('command:')
                if key in SETTING_KEYS:
                    try:
                        cmd = LakeShoreCommand(key, val)
                    except ValueError as e:
                        log.warning(f"Ignoring invalid command ('{key}={val}'): {e}")
                        continue
                    try:
                        log.info(f"Processing command '{cmd}'")
                        if key == HEATSWITCH_MOVE_KEY:
                            current_pos = redis.read(HEATSWITCH_POSITION_KEY)
                            if current_pos.lower() in ['opening', 'closing']:
                                # N.B. Don't try to reverse motion while the heatswitch is opening/closing
                                log.warning(f"Trying to send command {val} while heatswitch is {current_pos}. Command ignored!")
                            else:
                                log.info(f"Commanding heatswitch to {val} from heatswitch {current_pos}")
                                if val.lower() == "open":
                                    hs.open()
                                elif val.lower() == "close":
                                    hs.close()
                                else:
                                    log.warning("Illegal command that was not previously handled!")
                        elif key in [VELOCITY_KEY, RUNNING_CURRENT_KEY, ACCELERATION_KEY]:
                            hs.update_binary_setting(key, val)
                        elif key == STOP_KEY:
                            hs.stop()
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
