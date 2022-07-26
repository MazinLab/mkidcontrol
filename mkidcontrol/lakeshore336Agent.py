"""
Author: Noah Swimmer
21 February 2022

Program for communicating with and controlling the LakeShore336 Cryogenic Temperature Controller.

This module is responsible for reading out the non-device thermometry. This includes the intermediate stages (77K and
4K) and the 1K stage

N.B. Python API at https://lake-shore-python-driver.readthedocs.io/en/latest/model_336.html

TODO: Figure out how to 'block' settings if other settings are in place (e.g. Input range cannot be in V if sensor type is RTD)

TODO: Long term -> support adding curves

TODO: We will need 2 statuses in the flask app, the FIRST will be 'LS336 Status' which will either be good/bad/error/off/etc.
 The SECOND will be 'lakeshore336.service Status' which will be on/off/etc. To *ever* restart, one will need to restart
 the service, not anything within the program
"""

import sys
import logging
import time
import numpy as np

from mkidcontrol.mkidredis import RedisError
from mkidcontrol.devices import LakeShore336
import mkidcontrol.util as util
from mkidcontrol.commands import COMMANDS336, LakeShoreCommand, ENABLED_336_CHANNELS
import mkidcontrol.mkidredis as redis

log = logging.getLogger(__name__)

QUERY_INTERVAL = 1

SETTING_KEYS = tuple(COMMANDS336.keys())

DEVICE = '/dev/ls336'

TEMP_KEYS = ['status:temps:1k-stage:temp', 'status:temps:3k-stage:temp', 'status:temps:50k-stage:temp']
SENSOR_VALUE_KEYS = ['status:temps:1k-stage:resistance', 'status:temps:3k-stage:voltage', 'status:temps:50k-stage:voltage']

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


import wtforms
from wtforms.fields import *
from wtforms.widgets import HiddenInput
from wtforms.fields.html5 import *
from wtforms.validators import *
from wtforms import Form
from flask_wtf import FlaskForm
from serial import SerialException


class LS336Form(FlaskForm):
    title = "Lake Shore 336 Settings"
    set = SubmitField("Set Lake Shore")


class InputSensorForm(FlaskForm):
    from mkidcontrol.commands import LS336_INPUT_SENSOR_TYPES, LS336_INPUT_SENSOR_UNITS, LS336_AUTORANGE_VALUES, \
        LS336_COMPENSATION_VALUES
    channel = HiddenField("")
    name = StringField("Name")
    sensor_type = SelectField("Sensor Type", choices=list(LS336_INPUT_SENSOR_TYPES.keys()))
    units = SelectField("Units", choices=list(LS336_INPUT_SENSOR_UNITS.keys()))
    curve = SelectField("Curve", choices=np.arange(1, 60))
    autorange = SelectField(label="Autorange Enable", choices=list(LS336_AUTORANGE_VALUES.keys()))
    compensation = SelectField(label="Compensation", choices=list(LS336_COMPENSATION_VALUES.keys()))


class DiodeForm(InputSensorForm):
    from mkidcontrol.commands import LS336_DIODE_RANGE
    input_range = SelectField("Input Range", choices=list(LS336_DIODE_RANGE.keys()))
    update = SubmitField("Update")


class RTDForm(InputSensorForm):
    from mkidcontrol.commands import LS336_RTD_RANGE
    input_range = SelectField("Input Range", choices=list(LS336_RTD_RANGE.keys()))
    update = SubmitField("Update")


class DisabledInputForm(FlaskForm):
    from mkidcontrol.commands import LS336_INPUT_SENSOR_TYPES, LS336_INPUT_SENSOR_UNITS, LS336_INPUT_SENSOR_RANGE, \
        LS336_AUTORANGE_VALUES, LS336_COMPENSATION_VALUES
    channel = HiddenField()
    name = StringField("Name")
    sensor_type = SelectField("Sensor Type", choices=list(LS336_INPUT_SENSOR_TYPES.keys()))
    units = SelectField("Units", choices=list(LS336_INPUT_SENSOR_UNITS.keys()), render_kw={'disabled':True})
    curve = SelectField("Curve", choices=np.arange(1, 60), render_kw={'disabled':True})
    autorange = SelectField("Autorange Enable", choices=list(LS336_AUTORANGE_VALUES.keys()))
    compensation = SelectField("Compensation", choices=list(LS336_COMPENSATION_VALUES.keys()))
    input_range = SelectField("Input Range", choices=list(LS336_INPUT_SENSOR_RANGE.keys()), render_kw={'disabled':True})
    enable = SubmitField("Enable")


class Schedule(FlaskForm):
    at = DateTimeLocalField('Activate at', format='%m/%d/%Y %I:%M %p')
    # at = wtforms.DateTimeField('Activate at', format='%m/%d/%Y %I:%M %p')
    # date = wtforms.fields.html5.DateField('Date')
    # time = wtforms.fields.html5.TimeField('Time')
    repeat = BooleanField(label='Every Day?', default=True)
    clear = SubmitField("Clear")
    schedule = SubmitField("Set")


if __name__ == "__main__":

    util.setup_logging('lakeshore336Agent')
    redis.setup_redis(ts_keys=TS_KEYS)


    def callback(tvals, svals):
        vals = tvals + svals
        keys = TEMP_KEYS + SENSOR_VALUE_KEYS
        d = {k: x for k, x in zip(keys, vals) if x}
        redis.store(d, timeseries=True)


    try:
        lakeshore = LakeShore336('LakeShore336', port=DEVICE, enabled_channels=ENABLED_336_CHANNELS,
                                 initializer=initializer)
    except:
        lakeshore = LakeShore336('LakeShore336', enabled_channels=ENABLED_336_CHANNELS,
                                 initializer=initializer)

    # TODO: Allow monitor to gracefully fail if it is querying garbage
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
