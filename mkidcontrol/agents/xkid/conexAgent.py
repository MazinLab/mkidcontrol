"""
3 January 2023
Author: Noah Swimmer

Agent for controlling the CONEX-AG-M100D Piezo Motor Mirror Mount (https://www.newport.com/p/CONEX-AG-M100D) that serves
as a tip/tilt mirror for XKID aiding in both alignment and dithering.

Axis syntax is: U -> rotation around the y-axis, V -> rotation around the y-axis

Commands sent to conex for dithering/moving are dicts converted to strings via the json.dumps() to conveniently send
complicated dicts over redis pubsub connections

TODO: Add _wait4move and _wait4dither functionality to ConexController class to properly update dither status in
 flask GUI

TODO: Log dither starting
"""

import logging
import sys
import json

from mkidcontrol.mkidredis import RedisError
import mkidcontrol.mkidredis as redis
import mkidcontrol.util as util
from mkidcontrol.commands import COMMANDSCONEX, LakeShoreCommand
from mkidcontrol.devices import ConexController

logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger(__name__)

QUERY_INTERVAL = 1
TIMEMOUT = 2  # Timeout for query request

STATUS_KEY = "status:device:conex:status"
SN_KEY = "status:device:conex:sn"
FIRMWARE_KEY = "status:device:conex:firmware"

CONEX_STATUS_KEY = "status:device:conex:controller-status"
MOVE_STATUS_KEY = "status:device:conex:move-status"
DITHER_STATUS_KEY = "status:device:conex:dither-status"

MOVE_COMMAND_KEY = "conex:move"
DITHER_COMMAND_KEY = "conex:dither"
STOP_COMMAND_KEY = "conex:stop"

ENABLE_CONEX_KEY = "device-settings:conex:enabled"

CONEX_COMMANDS = tuple([MOVE_COMMAND_KEY, DITHER_COMMAND_KEY, STOP_COMMAND_KEY])

SETTING_KEYS = tuple(COMMANDSCONEX.keys())
COMMAND_KEYS = tuple([f"command:{key}" for key in list(SETTING_KEYS) + list(CONEX_COMMANDS)])


def callback(move_status, dither_status, conex_status):
    statuses = [move_status, dither_status, conex_status]
    vals = [json.dumps(status) for status in statuses]
    keys = [MOVE_STATUS_KEY, DITHER_STATUS_KEY, CONEX_STATUS_KEY]
    d = {k: x for k, x in zip(keys, vals)}
    try:
        if all(i is None for i in statuses):
            redis.store({STATUS_KEY: "Error"})
        else:
            redis.store(d)
            redis.store({STATUS_KEY: "OK"})
    except RedisError:
        log.warning('Storing LakeShore336 data to redis failed!')


if __name__ == "__main__":

    redis.setup_redis()
    util.setup_logging('conexAgent')

    try:
        cc = ConexController(port='/dev/conex')
        redis.store({SN_KEY: cc.conex.id_number})
        redis.store({FIRMWARE_KEY: cc.conex.firmware})
        redis.store({STATUS_KEY: "OK"})
    except RedisError as e:
        log.error(f"Redis server error! {e}")
        sys.exit(1)
    except Exception as e:
        log.critical(f"Could not connect to the conex! Error {e}")
        redis.store({STATUS_KEY: f"Error: {e}"})
        sys.exit(1)

    # TODO: This will overwrite the status and make it impossible to tell what is going on with the conex.
    #  This requires more thought and incorporation of the wait4dither/wait4move status updates
    # cc.monitor(QUERY_INTERVAL, (cc.queryMove, cc.queryDither, cc.status), value_callback=callback)

    # N.B. Conex movement/dither commands will be dicts turned into strings via json.dumps() for convenient sending and
    # ultimately reformatting over redis and flask connections.
    try:
        while True:
            for key, val in redis.listen(COMMAND_KEYS):
                log.debug(f"conexAgent received {key}: {val}.")
                key = key.removeprefix("command:")
                try:
                    if key in SETTING_KEYS:
                        try:
                            cmd = LakeShoreCommand(key, val)
                        except ValueError as e:
                            log.warning(f"Ignoring invalid command ('{key}={val}'): {e}")
                            continue
                        log.info(f"Processing command {cmd}")
                        if key == ENABLE_CONEX_KEY:
                            if val.lower() == 'enabled':
                                log.debug("Enabling Conex...")
                                cc.conex.enable()
                                log.info("Conex enabled")
                            elif val.lower() == 'disabled':
                                log.debug("Disabling Conex")
                                cc.conex.disable()
                                log.info("Conex disabled")
                            redis.store({cmd.setting: cmd.value})
                            redis.store({STATUS_KEY: "OK"})
                    elif key == MOVE_COMMAND_KEY:
                        log.debug(f"Starting conex move...")
                        val = json.loads(val)
                        cc.start_move(val['x'], val['y'])
                        redis.store({STATUS_KEY: "OK"})
                        log.info(f"Conex move to ({val['x']}, {val['y']}) successful")
                    elif key == DITHER_COMMAND_KEY:
                        log.debug(f"Starting dither...")
                        val = json.loads(val)
                        cc.start_dither(val)
                        redis.store({STATUS_KEY: "OK"})
                        log.info(f"Started dither with params: {val}")
                    elif key == STOP_COMMAND_KEY:
                        log.debug("Stopping conex")
                        cc.stop()
                        redis.store({STATUS_KEY: "OK"})
                        log.info("Conex stopped!")
                except IOError as e:
                    redis.store({STATUS_KEY: f"Error {e}"})
                    log.error(f"Comm error: {e}")
    except RedisError as e:
        log.error(f"Redis server error! {e}")
        sys.exit(1)

