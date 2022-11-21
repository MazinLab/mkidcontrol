"""
Author: Noah Swimmer
21 February 2022

Program for communicating with and controlling the LakeShore372 AC Resistance Bridge.

This module is responsible for reading out the device temperature and providing the signal in the PID loop which will
ultimately control the thermometer. The signal will be sent to the Lake Shore 625 magnet controller for producing a
control current in the magnet.

From the manual -> Do not query more than 20 times per second

N.B. Python API at https://lake-shore-python-driver.readthedocs.io/en/latest/model_372.html

TODO: 'Block' settings (e.g. excitation cannot be in V if mode is Current)

TODO: Gracefully handle serial disconnects/reconnects
"""

import sys
import logging
import time
import numpy as np

from mkidcontrol.mkidredis import RedisError
from mkidcontrol.devices import LakeShore372, InstrumentException
import mkidcontrol.util as util
from mkidcontrol.commands import COMMANDS372, LakeShoreCommand, ENABLED_372_INPUT_CHANNELS
import mkidcontrol.mkidredis as redis

log = logging.getLogger()

TEMPERATURE_KEYS = ['status:temps:device-stage:temp', 'status:temps:1k-stage:temp']
RESISTANCE_KEYS = ['status:temps:device-stage:resistance', 'status:temps:1k-stage:resistance']
EXCITATION_POWER_KEYS = ['status:temps:device-stage:excitation-power', 'status:temps:1k-stage:excitation-power']

OUTPUT_VOLTAGE_KEY = 'status:device:ls372:output-voltage'

REGULATION_TEMP_KEY = "device-settings:mkidarray:regulating-temp"

TS_KEYS = TEMPERATURE_KEYS + RESISTANCE_KEYS + EXCITATION_POWER_KEYS + list(OUTPUT_VOLTAGE_KEY)

STATUS_KEY = 'status:device:ls372:status'
FIRMWARE_KEY = "status:device:ls372:firmware"
MODEL_KEY = 'status:device:ls372:model'
SN_KEY = 'status:device:ls372:sn'

QUERY_INTERVAL = 1

SETTING_KEYS = tuple(COMMANDS372.keys())

COMMAND_KEYS = [f"command:{k}" for k in SETTING_KEYS]

OUTPUT_MODE_KEY = 'device-settings:ls372:heater-channel-0:output-mode'
OUTPUT_MODE_COMMAND_KEY = f"command:{OUTPUT_MODE_KEY}"
OUTPUT_RANGE_KEY = 'device-settings:ls372:heater-channel-0:range'
OUTPUT_RANGE_COMMAND_KEY = f"command:{OUTPUT_RANGE_KEY}"


def to_pid_output():
    redis.publish(OUTPUT_MODE_COMMAND_KEY, "CLOSED_LOOP", store=False)


def to_no_output():
    redis.publish(OUTPUT_MODE_COMMAND_KEY, "OFF", store=False)


def in_pid_output():
    return redis.read(OUTPUT_MODE_KEY) == "CLOSED_LOOP"


def in_no_output():
    return redis.read(OUTPUT_MODE_KEY) == "OFF"


def turn_on_heater_output(output_range='1 mA'):
    redis.publish(OUTPUT_RANGE_COMMAND_KEY, output_range, store=False)


def heater_output_on():
    return redis.read(OUTPUT_RANGE_KEY) != '0'


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
        log.critical('Unable to pull settings from redis to initialize LS372')
        raise IOError(e)
    except KeyError as e:
        log.critical('Unable to pull setting {e} from redis to initialize LS372')
        raise IOError(e)

    try:
        redis.store(initialized_settings)
    except RedisError:
        log.warning('Storing device settings to redis failed')


def callback(temps, ress, exs, ov):
    vals = temps + ress + exs + [ov]
    keys = TEMPERATURE_KEYS + RESISTANCE_KEYS + EXCITATION_POWER_KEYS + [OUTPUT_VOLTAGE_KEY]

    d = {k: x for k, x in zip(keys, vals)}
    try:
        if all(i is None for i in vals):
            redis.store({STATUS_KEY: "Error"})
        else:
            redis.store(d, timeseries=True)
            redis.store({STATUS_KEY: "OK"})
    except RedisError:
        log.warning('Storing LakeShore372 data to redis failed!')


if __name__ == "__main__":

    util.setup_logging('lakeshore372Agent')
    redis.setup_redis(ts_keys=TS_KEYS)

    try:
        log.debug(f"Connecting to LakeShore 372...")
        try:
            lakeshore = LakeShore372('LakeShore372', baudrate=57600, port='/dev/ls372',
                                     enabled_input_channels=ENABLED_372_INPUT_CHANNELS)#, initializer=initializer)
            log.info(f"LakeShore 372 connection successful!")
            to_pid_output()
            turn_on_heater_output()
            redis.store({STATUS_KEY: "OK"})
        except InstrumentException:
            log.info(f"Instrument exception occurred, trying to connect from PID/VID")
            lakeshore = LakeShore372('LakeShore372', baudrate=57600,
                                     enabled_input_channels=ENABLED_372_INPUT_CHANNELS)#, initializer=initializer)
            log.info(f"LakeShore 372 connection successful!")
            redis.store({STATUS_KEY: "OK"})
    except IOError as e:
        log.critical(f"Error in connecting to LakeShore 372: {e}")
        redis.store({STATUS_KEY: "Error"})
        sys.exit(1)
    except RedisError as e:
        log.critical(f"Error in communicating with redis: {e}")
        sys.exit(1)

    lakeshore.monitor(QUERY_INTERVAL, (lakeshore.temp, lakeshore.sensor_vals, lakeshore.excitation_power, lakeshore.output_voltage), value_callback=callback)

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
                    if cmd.command_code == "SETP":
                        redis.publish({REGULATION_TEMP_KEY: cmd.command_value}, store=False)
                    redis.store({STATUS_KEY: "OK"})
                except IOError as e:
                    redis.store({STATUS_KEY: f"Error"})
                    log.error(f"Comm error: {e}")
    except RedisError as e:
        log.critical(f"Redis server error! {e}")
        sys.exit(1)
