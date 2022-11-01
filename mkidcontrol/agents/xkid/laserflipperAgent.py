"""
Author: Joseph Redford Oct 21st 2022

Program to control ArduinoUNO that will turn on and off the laser diodes in the
calibration box. Code copied and modified from Noah's currentduinoAgent

TODO: Test + update for redis schema
"""

import logging
import sys

from mkidcontrol.devices import Laserflipperduino
import mkidcontrol.mkidredis as redis
from mkidcontrol.mkidredis import RedisError
from mkidcontrol.commands import COMMANDSLASERFLIPPER, SimCommand
import mkidcontrol.util as util

logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger(__name__)

QUERY_INTERVAL = 1

laser_vals = ['808', '904', '980', '1120', '1310']
names = [f"{val} nm" for val in laser_vals]
names.append('mirror')

STATUS_KEY = "status:device:laserflipperduino:status"
FIRMWARE_KEY = "status:device:laserflipperduino:firmware"

SETTING_KEYS = tuple(COMMANDSLASERFLIPPER.keys())
COMMAND_KEYS = (f"command:{key}" for key in SETTING_KEYS)

MIRROR_FLIP_KEY = 'device-settings:laserflipperduino:flipper:position'
LASER_KEYS = ('device-settings:laserflipperduino:laserbox:808:power',
              'device-settings:laserflipperduino:laserbox:904:power',
              'device-settings:laserflipperduino:laserbox:980:power',
              'device-settings:laserflipperduino:laserbox:1120:power',
              'device-settings:laserflipperduino:laserbox:1310:power'
              )

def callback(values):
    vals = values.values()
    d = {k: x for k, x in zip(SETTING_KEYS, vals)}
    try:
        if all(i is None for i in vals):
            redis.store({STATUS_KEY: "Error"})
        else:
            redis.store(d, timeseries=True)
            redis.store({STATUS_KEY: "OK"})
    except RedisError:
        log.warning('Storing LakeShore336 data to redis failed!')


if __name__ == "__main__":

    redis.setup_redis()
    util.setup_logging('laserflipperAgent')

    try:
        laserduino = Laserflipperduino(port='/dev/laserflipper')
        redis.store({STATUS_KEY: "OK"})
    except RedisError as e:
        log.error(f"Redis server error! {e}")
        sys.exit(1)
    except Exception as e:
        log.critical(f"Could not connect to the laserflipper! Error {e}")
        redis.store({STATUS_KEY: f"Error: {e}"})
        sys.exit(1)

    laserduino.monitor(QUERY_INTERVAL, (laserduino.statuses, ), value_callback=callback)

    try:
        while True:
            for key, val in redis.listen(COMMAND_KEYS):
                log.debug(f"LaserflipperAgent received {key}: {val}.")
                key = key.removeprefix("command:")
                if key in SETTING_KEYS:
                    try:
                        cmd = SimCommand(key, val)
                    except ValueError as e:
                        log.warning(f"Ignoring invalid command ('{key}={val}'): {e}")
                        continue
                    try:
                        log.info(f"Processing command 'cmd'")
                        if key == MIRROR_FLIP_KEY:
                            laserduino.set_mirror_position(cmd.value)
                        elif key in LASER_KEYS:
                            laserduino.set_diode(int(cmd.command), int(cmd.value))
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
