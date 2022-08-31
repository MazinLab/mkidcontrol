"""
Author: Noah Swimmer
21 February 2022

Program for communicating with and controlling the LakeShore336 Cryogenic Temperature Controller.

This module is responsible for reading out the non-device thermometry. This includes the intermediate stages (77K and
4K) and the 1K stage

N.B. Python API at https://lake-shore-python-driver.readthedocs.io/en/latest/model_336.html

TODO: Figure out how to 'block' settings if other settings are in place (e.g. Input range cannot be in V if sensor type is RTD)

TODO: Long term -> support adding curves

TODO: Gracefully handle restarts
"""

import sys
import logging
import time
import numpy as np

from mkidcontrol.mkidredis import RedisError
from mkidcontrol.devices import LakeShore336, InstrumentException
import mkidcontrol.util as util
from mkidcontrol.commands import COMMANDS336, LakeShoreCommand, ENABLED_336_CHANNELS
import mkidcontrol.mkidredis as redis

log = logging.getLogger(__name__)

QUERY_INTERVAL = 1

SETTING_KEYS = tuple(COMMANDS336.keys())

DEVICE = '/dev/ls336'

TEMP_KEYS = ['status:temps:3k-stage:temp', 'status:temps:50k-stage:temp']
SENSOR_VALUE_KEYS = ['status:temps:3k-stage:voltage', 'status:temps:50k-stage:voltage']

TS_KEYS = TEMP_KEYS + SENSOR_VALUE_KEYS

STATUS_KEY = 'status:device:ls336:status'
FIRMWARE_KEY = "status:device:ls336:firmware"
MODEL_KEY = 'status:device:ls336:model'
SN_KEY = 'status:device:ls336:sn'

COMMAND_KEYS = [f"command:{k}" for k in SETTING_KEYS]

def firmware_pull(device):
    # Grab and store device info
    try:
        info = device.device_info
        d = {FIRMWARE_KEY: info['firmware'], MODEL_KEY: info['model'], SN_KEY: info['sn']}
    except IOError as e:
        log.error(f"When checking device info: {e}")
        d = {FIRMWARE_KEY: '', MODEL_KEY: '', SN_KEY: ''}

    try:
        redis.store(d)
    except RedisError:
        log.warning('Storing device info to redis failed')


def initializer(device):
    """
    Callback run on connection to the sim whenever it is not initialized. This will only happen if the sim loses all
    of its settings, which should never every happen. Any settings applied take immediate effect
    """
    firmware_pull(device)
    try:
        settings_to_load = redis.read(SETTING_KEYS, error_missing=True)
        initialized_settings = device.apply_schema_settings(settings_to_load)
        time.sleep(1)
    except RedisError as e:
        log.critical('Unable to pull settings from redis to initialize sim960')
        raise IOError(e)
    except KeyError as e:
        log.critical('Unable to pull setting {e} from redis to initialize sim960')
        raise IOError(e)

    try:
        redis.store(initialized_settings)
    except RedisError:
        log.warning('Storing device settings to redis failed')


def callback(tvals, svals):
    vals = tvals + svals
    keys = TEMP_KEYS + SENSOR_VALUE_KEYS
    d = {k: x for k, x in zip(keys, vals)}
    try:
        if all(i is None for i in vals):
            redis.store({STATUS_KEY: "Error"})
        else:
            redis.store(d, timeseries=True)
            redis.store({STATUS_KEY: "OK"})
    except RedisError:
        log.warning('Storing LakeShore336 data to redis failed!')


if __name__ == "__main__":

    util.setup_logging('lakeshore336Agent')
    redis.setup_redis(ts_keys=TS_KEYS)


    try:
        log.debug(f"Connecting to LakeShore 336")
        try:
            lakeshore = LakeShore336('LakeShore336', port=DEVICE, enabled_channels=ENABLED_336_CHANNELS)#,
                                     # initializer=initializer)
            log.info(f"LakeShore 336 connection successful!")
            redis.store({STATUS_KEY: "OK"})
        except InstrumentException:
            log.info(f"Instrument exception occurred, trying to connect from PID/VID")
            lakeshore = LakeShore336('LakeShore336', enabled_channels=ENABLED_336_CHANNELS)#,
                                     # initializer=initializer)
            log.info(f"Lake Shore 336 connection successful!")
            redis.store({STATUS_KEY: "OK"})
    except IOError as e:
        log.critical(f"Error in connecting to LakeShore 336: {e}")
        redis.store({STATUS_KEY: "Error"})
        sys.exit(1)
    except RedisError as e:
        log.critical(f"Error in communicating with redis: {e}")
        sys.exit(1)

    lakeshore.monitor(QUERY_INTERVAL, (lakeshore.temp, lakeshore.sensor_vals), value_callback=callback)

    try:
        while True:
            for key, val in redis.listen(COMMAND_KEYS):
                log.debug(f"heard {key} -> {val}!")
                try:
                    cmd = LakeShoreCommand(key.removeprefix('command:'), val)
                except ValueError as e:
                    log.warning(f"Ignoring invalid command ('{key}={val}'): {e}")
                    continue
                try:
                    lakeshore.handle_command(cmd)
                    redis.store({cmd.setting: cmd.value})
                    redis.store({STATUS_KEY: "OK"})
                except IOError as e:
                    redis.store({STATUS_KEY: f"Error {e}"})
                    log.error(f"Comm error: {e}")
    except RedisError as e:
        log.critical(f"Redis server error! {e}")
        sys.exit(1)
