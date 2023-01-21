"""
Author: Joseph Redford Oct 21st 2022

Program to control ArduinoUNO that will turn on and off the laser diodes in the
calibration box. Code copied and modified from Noah's currentduinoAgent
"""

import logging
import sys

import mkidcontrol.mkidredis as redis
import mkidcontrol.util as util
from mkidcontrol.mkidredis import RedisError
from mkidcontrol.devices import Laserflipperduino
from mkidcontrol.commands import COMMANDSLASERFLIPPER, LakeShoreCommand

logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger(__name__)

QUERY_INTERVAL = 1

LASER_VALS = ['808', '904', '980', '1120', '1310']
NAMES = [f"{val} nm" for val in LASER_VALS]
NAMES.append('mirror')

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
LASER_ENABLE_KEYS = ('device-settings:laserflipperduino:laserbox:808:enabled',
                     'device-settings:laserflipperduino:laserbox:904:enabled',
                     'device-settings:laserflipperduino:laserbox:980:enabled',
                     'device-settings:laserflipperduino:laserbox:1120:enabled',
                     'device-settings:laserflipperduino:laserbox:1310:enabled'
                     )

if __name__ == "__main__":

    redis.setup_redis()
    util.setup_logging('laserflipperAgent')

    try:
        laserduino = Laserflipperduino(port='/dev/laserflipper', lasernames=NAMES)
        redis.store({STATUS_KEY: "OK"})
    except RedisError as e:
        log.error(f"Redis server error! {e}")
        sys.exit(1)
    except Exception as e:
        log.critical(f"Could not connect to the laserflipper! Error {e}")
        redis.store({STATUS_KEY: f"Error: {e}"})
        sys.exit(1)

    try:
        while True:
            for key, val in redis.listen(COMMAND_KEYS):
                log.debug(f"LaserflipperAgent received {key}: {val}.")
                key = key.removeprefix("command:")
                if key in SETTING_KEYS:
                    try:
                        cmd = LakeShoreCommand(key, val)
                    except ValueError as e:
                        log.warning(f"Ignoring invalid command ('{key}={val}'): {e}")
                        continue
                    try:
                        log.info(f"Processing command 'cmd'")
                        if key == MIRROR_FLIP_KEY:
                            laserduino.set_mirror_position(cmd.value)
                        elif key in LASER_KEYS:
                            laserduino.set_diode(int(cmd.command), int(cmd.value))
                        redis.store({cmd.setting: cmd.value})
                        redis.store({STATUS_KEY: "OK"})
                    except IOError as e:
                        redis.store({STATUS_KEY: f"Error {e}"})
                        log.error(f"Comm error: {e}")
    except RedisError as e:
        log.error(f"Redis server error! {e}")
        sys.exit(1)
