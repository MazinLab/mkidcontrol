"""
Author: Noah Swimmer, 26 October 2022

Agent for controlling the Thorlabs TDC001 + MTS50/M-Z8 (50 mm) motor slider which will control the focus position

For the TDC001 source code see the readthedocs page at https://thorlabs-apt-device.readthedocs.io/en/latest/api/thorlabs_apt_device.devices.aptdevice_motor.html
For the MTS50/M-Z8 manual see https://www.thorlabs.com/drawings/b0f5ad357fd27d60-4B9598C7-C024-7FC8-D2B6ACA417A30171/MTS50_M-Z8-Manual.pdf

MTS50/M-Z8 NOTES:
    - The slider has 50 mm of movement.
    - There are 512 encoder counts per revolution of the motor. The motor shaft goes to a 67.49:1 planetary gear head.
    The motor must then rotate 67.49 times to rotate the 1.0 mm pitch screw once (i.e. move the slider by 1.0 mm)
    - There are 512 x 67.49 = 34,555 encoder steps per revolution of the lead screw
    - Each encode count is 1.0 mm / 34,555 encoder steps = 29 nm / encoder step
    - The device can move from 0 - 1727750 (in encoder step space) or 0 - 50 (in mm space)
"""

import logging
import sys

from mkidcontrol.mkidredis import RedisError
import mkidcontrol.mkidredis as redis
import mkidcontrol.util as util
from mkidcontrol.commands import COMMANDSFOCUS, LakeShoreCommand
from mkidcontrol.devices import Focus

DEFAULT_PARAMS = {'home': [[{'home_dir': 2,
    'limit_switch': 1,
    'home_velocity': 137439,
    'offset_distance': 24576,
    'msg': 'mot_get_homeparams',
    'msgid': 1090,
    'dest': 0,
    'source': 0,
    'chan_ident': 1}]],
 'jog': {'jog_mode': 2,
  'step_size': 12288,
  'min_velocity': 0,
  'acceleration': 75,
  'max_velocity': 164931,
  'stop_mode': 2,
  'msg': 'mot_get_jogparams',
  'msgid': 1048,
  'source': 0,
  'dest': 0,
  'chan_ident': 1},
 'move': {'backlash_distance': 246,
  'msg': 'mot_get_genmoveparams',
  'msgid': 1084,
  'source': 0,
  'dest': 0,
  'chan_ident': 1},
 'velocity': {'min_velocity': 0,
  'max_velocity': 164931,
  'acceleration': 75,
  'msg': 'mot_get_velparams',
  'msgid': 1045,
  'source': 0,
  'dest': 0,
  'chan_ident': 1}}

logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger("focusAgent")
from logging import getLogger
l = getLogger('thorlabs_apt_device')
l.setLevel("DEBUG")

QUERY_INTERVAL = 1

ENCODER_STEPS_PER_MM = 34555

STATUS_KEY = "status:device:focus:status"

FOCUS_POSITION_MM_KEY = 'status:device:focus:position-mm'
FOCUS_POSITION_ENCODER_KEY = 'status:device:focus:position-encoder'

TS_KEYS = (FOCUS_POSITION_MM_KEY, FOCUS_POSITION_ENCODER_KEY)

MOVE_BY_MM_KEY = 'device-settings:focus:desired-move:mm'
MOVE_BY_ENC_KEY = 'device-settings:focus:desired-move:encoder'
MOVE_TO_MM_KEY = 'device-settings:focus:desired-position:mm'
MOVE_TO_ENC_KEY = 'device-settings:focus:desired-position:encoder'
HOME_KEY = 'device-settings:focus:home'
JOG_KEY = 'device-settings:focus:jog'

SETTING_KEYS = tuple(COMMANDSFOCUS.keys())
COMMAND_KEYS = tuple([f"command:{key}" for key in list(SETTING_KEYS) + [MOVE_BY_MM_KEY, MOVE_BY_ENC_KEY, JOG_KEY,
                                                                        HOME_KEY, MOVE_TO_MM_KEY, MOVE_TO_ENC_KEY]])

def callback(pos):
    vals = [pos['mm'], pos['encoder']]
    keys = [FOCUS_POSITION_MM_KEY, FOCUS_POSITION_ENCODER_KEY]
    d = {k: x for k, x in zip(keys, vals)}
    try:
        if all(i is None for i in vals):
            redis.store({STATUS_KEY: "Error"})
        else:
            redis.store(d, timeseries=True)
            redis.store({STATUS_KEY: "OK"})
    except RedisError:
        log.warning('Storing filter wheel data to redis failed!')


if __name__ == "__main__":
    redis.setup_redis(ts_keys=TS_KEYS)
    util.setup_logging('focusAgent')

    try:
        f = Focus(name='focus', port='/dev/focus')
        redis.store({STATUS_KEY: "OK"})
    except RedisError as e:
        log.error(f"Redis server error! {e}")
        sys.exit(1)
    except Exception as e:
        log.critical(f"Could not connect to the filter wheel! Error {e}")
        redis.store({STATUS_KEY: f"Error: {e}"})
        sys.exit(1)

    # f.monitor(QUERY_INTERVAL, (f.position, ), value_callback=callback)

    try:
        while True:
            for key, val in redis.listen(COMMAND_KEYS):
                key = key.removeprefix('command:')
                log.debug(f"focusAgent received {key} -> {val}!")
                try:
                    cmd = LakeShoreCommand(key, val)
                except ValueError as e:
                    log.warning(f"Ignoring invalid command ('{key}={val}'): {e}")
                    pass
                try:
                    if 'params' in key:
                        f.update_param(key, val)
                        redis.store({cmd.setting: cmd.value})
                        redis.store({STATUS_KEY: "OK"})
                    elif 'desired-position' in key:
                        units = key.split(":")[-1]
                        f.move_to(val, units=units)
                    elif key in [MOVE_BY_MM_KEY, MOVE_BY_ENC_KEY]:
                        units = key.split(":")[-1]
                        f.move_by(val, units=units)
                    elif key == JOG_KEY:
                        f.jog(direction=val)
                    elif key == HOME_KEY:
                        f.home()
                except IOError as e:
                    redis.store({STATUS_KEY: f"Error {e}"})
                    log.error(f"Comm error: {e}")
    except RedisError as e:
        log.critical(f"Redis server error! {e}")
        sys.exit(1)

