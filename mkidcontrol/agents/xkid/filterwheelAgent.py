"""
Author: Noah Swimmer, 28 October 2022

Code to control the Finger Lakes Instrumentation (FLI) CFW2-7 Filter wheel

TODO: Consider not reinitializing to 'closed' if that is the last known position. Otherwise a homing move will cause it
 to go all the way around meaning it'll open when you don't necessarily want it to

TODO: Make 'close' and 'open' helper functions

TODO: Logging level probably works better at INFO
"""

import logging
import sys

import mkidcontrol.mkidredis as redis
import mkidcontrol.util as util
from mkidcontrol.mkidredis import RedisError
from mkidcontrol.devices import FilterWheel
from mkidcontrol.commands import COMMANDSFILTERWHEEL, LakeShoreCommand

logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger(__name__)


QUERY_INTERVAL = 1

STATUS_KEY = "status:device:filterwheel:status"
SN_KEY = "status:device:filterwheel:sn"
MODEL_KEY = "status:device:filterwheel:model"

SETTING_KEYS = tuple(COMMANDSFILTERWHEEL.keys())
COMMAND_KEYS = (f"command:{key}" for key in SETTING_KEYS)

FILTERWHEEL_POSITION_KEY = 'device-settings:filterwheel:position'
FILTERWHEEL_FILTER_KEY = 'device-settings:filterwheel:filter'

FILTERS = {0: 'Closed',
           1: 'Y',
           2: 'Zs',
           3: 'J',
           4: '220+125',
           5: '125',
           6: 'Open'}

if __name__ == "__main__":

    redis.setup_redis()
    util.setup_logging('filterwheelAgent')

    try:
        fw = FilterWheel('filterwheel', b'/dev/filterwheel', filters=FILTERS)
        redis.store({MODEL_KEY: fw.model})
        redis.store({SN_KEY: fw.serial_number})
        redis.store({STATUS_KEY: "OK"})
    except RedisError as e:
        log.error(f"Redis server error! {e}")
        sys.exit(1)
    except Exception as e:
        log.critical(f"Could not connect to the filter wheel! Error {e}")
        redis.store({STATUS_KEY: f"Error: {e}"})
        sys.exit(1)

    try:
        while True:
            for key, val in redis.listen(COMMAND_KEYS):
                log.debug(f"filterwheelAgent received {key}: {val}.")
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

