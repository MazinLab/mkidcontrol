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

TODO: Output voltage key value to report the control voltage to the lakeshore 625 magnet current control
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


def to_pid_output():
    redis.publish(OUTPUT_MODE_COMMAND_KEY, "CLOSED_LOOP", store=False)


def to_no_output():
    redis.publish(OUTPUT_MODE_COMMAND_KEY, "OFF", store=False)


def in_pid_output():
    return redis.read(OUTPUT_MODE_KEY) == "CLOSED_LOOP"


def in_no_output():
    return redis.read(OUTPUT_MODE_KEY) == "OFF"


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


import wtforms
from wtforms.fields import *
from wtforms.widgets import HiddenInput
from wtforms.fields.html5 import *
from wtforms.validators import *
from wtforms import Form
from flask_wtf import FlaskForm
from serial import SerialException


class ControlSensorForm(FlaskForm):
    from mkidcontrol.commands import LS372_SENSOR_MODE, LS372_AUTORANGE_VALUES, LS372_CONTROL_INPUT_CURRENT_RANGE, \
        LS372_CURRENT_SOURCE_SHUNTED_VALUES, LS372_INPUT_SENSOR_UNITS, LS372_RESISTANCE_RANGE, LS372_ENABLED_VALUES, \
        LS372_CURVE_COEFFICIENTS
    channel = HiddenField("")
    name = StringField("Name")
    mode = SelectField("Sensor Mode", default="Current", choices=list(LS372_SENSOR_MODE.keys()), render_kw={'disabled': True})
    excitation_range = SelectField("Excitation Current", choices=list(LS372_CONTROL_INPUT_CURRENT_RANGE.keys()))
    curve_number = SelectField("Curve", choices=np.arange(1, 60))
    auto_range = SelectField("Autorange Enable", choices=list(LS372_AUTORANGE_VALUES.keys()))
    current_source_shunted = SelectField("Current Source Shunted", choices=list(LS372_CURRENT_SOURCE_SHUNTED_VALUES.keys()))
    units = SelectField("Units", choices=list(LS372_INPUT_SENSOR_UNITS.keys()))
    resistance_range = SelectField("Resistance Range", default='63.2 k\u03A9', choices=list(LS372_RESISTANCE_RANGE.keys()), render_kw={'disabled': True})
    dwell_time = IntegerField("Dwell Time", default=1.0, validators=[NumberRange(1, 200)])
    pause_time = IntegerField("Pause Time", default=3.0, validators=[NumberRange(3, 200)])
    temperature_coefficient = SelectField("Temperature Coefficient", choices=list(LS372_CURVE_COEFFICIENTS.keys()))
    enable = SelectField("Enable", choices=list(LS372_ENABLED_VALUES.keys()))
    update = SubmitField("Update")


class DiasbledControlSensorForm(FlaskForm):
    from mkidcontrol.commands import LS372_SENSOR_MODE, LS372_AUTORANGE_VALUES, LS372_CONTROL_INPUT_CURRENT_RANGE, \
        LS372_CURRENT_SOURCE_SHUNTED_VALUES, LS372_INPUT_SENSOR_UNITS, LS372_RESISTANCE_RANGE, LS372_ENABLED_VALUES, \
        LS372_CURVE_COEFFICIENTS
    channel = HiddenField("")
    name = StringField("Name", render_kw={'disabled': True})
    mode = SelectField("Sensor Mode", default="Current", choices=list(LS372_SENSOR_MODE.keys()), render_kw={'disabled': True})
    excitation_range = SelectField("Excitation Current", choices=list(LS372_CONTROL_INPUT_CURRENT_RANGE.keys()), render_kw={'disabled': True})
    curve_number = SelectField("Curve", choices=np.arange(1, 60), render_kw={'disabled': True})
    auto_range = SelectField("Autorange Enable", choices=list(LS372_AUTORANGE_VALUES.keys()), render_kw={'disabled': True})
    current_source_shunted = SelectField("Current Source Shunted", choices=list(LS372_CURRENT_SOURCE_SHUNTED_VALUES.keys()), render_kw={'disabled': True})
    units = SelectField("Units", choices=list(LS372_INPUT_SENSOR_UNITS.keys()), render_kw={'disabled': True})
    resistance_range = SelectField("Resistance Range", choices=list(LS372_RESISTANCE_RANGE.keys()), render_kw={'disabled': True})
    dwell_time = IntegerField("Dwell Time", default=1, validators=[NumberRange(1, 200)], render_kw={'disabled': True})
    pause_time = IntegerField("Pause Time", default=3, validators=[NumberRange(3, 200)], render_kw={'disabled': True})
    temperature_coefficient = SelectField("Temperature Coefficient", choices=list(LS372_CURVE_COEFFICIENTS.keys()), render_kw={'disabled': True})
    enable = SelectField("Enable", choices=list(LS372_ENABLED_VALUES.keys()))
    update = SubmitField("Update")


class InputSensorForm(FlaskForm):
    from mkidcontrol.commands import LS372_SENSOR_MODE, LS372_AUTORANGE_VALUES, LS372_MEASUREMENT_INPUT_VOLTAGE_RANGE, \
        LS372_MEASUREMENT_INPUT_CURRENT_RANGE, LS372_CURRENT_SOURCE_SHUNTED_VALUES, LS372_INPUT_SENSOR_UNITS, \
        LS372_RESISTANCE_RANGE, LS372_ENABLED_VALUES, LS372_CURVE_COEFFICIENTS
    channel = HiddenField("")
    name = StringField("Name")
    mode = SelectField("Sensor Mode", default="Current", choices=list(LS372_SENSOR_MODE.keys()), render_kw={'disabled':True})
    excitation_range = SelectField("Excitation Current", choices=list(LS372_MEASUREMENT_INPUT_CURRENT_RANGE.keys()))
    curve_number = SelectField("Curve", choices=np.arange(1, 60))
    auto_range = SelectField(label="Autorange Enable", choices=list(LS372_AUTORANGE_VALUES.keys()))
    current_source_shunted = SelectField(label="Current Source Shunted", choices=list(LS372_CURRENT_SOURCE_SHUNTED_VALUES.keys()))
    units = SelectField(label="Units", choices=list(LS372_INPUT_SENSOR_UNITS.keys()))
    resistance_range = SelectField(label="Resistance Range", choices=list(LS372_RESISTANCE_RANGE.keys()))
    dwell_time = IntegerField(label="Dwell Time", default=1, validators=[NumberRange(1, 200)])
    pause_time = IntegerField(label="Pause Time", default=3, validators=[NumberRange(3, 200)])
    temperature_coefficient = SelectField(label="Temperature Coefficient", choices=list(LS372_CURVE_COEFFICIENTS))
    enable = SelectField(label="Enable", choices=list(LS372_ENABLED_VALUES.keys()))
    update = SubmitField("Update")


class DisabledEnabledInputSensorForm(FlaskForm):
    from mkidcontrol.commands import LS372_SENSOR_MODE, LS372_AUTORANGE_VALUES, LS372_MEASUREMENT_INPUT_VOLTAGE_RANGE, \
        LS372_MEASUREMENT_INPUT_CURRENT_RANGE, LS372_CURRENT_SOURCE_SHUNTED_VALUES, LS372_INPUT_SENSOR_UNITS, \
        LS372_RESISTANCE_RANGE, LS372_ENABLED_VALUES, LS372_CURVE_COEFFICIENTS

    channel = HiddenField("")
    name = StringField("Name")
    mode = SelectField("Sensor Mode", choices=list(LS372_SENSOR_MODE.keys()))
    excitation_range = SelectField("Excitation Current", choices=list(LS372_MEASUREMENT_INPUT_CURRENT_RANGE.keys()))
    curve_number = SelectField("Curve", choices=np.arange(1, 60))
    auto_range = SelectField(label="Autorange Enable", choices=list(LS372_AUTORANGE_VALUES.keys()))
    current_source_shunted = SelectField (label="Current Source Shunted",choices=list(LS372_CURRENT_SOURCE_SHUNTED_VALUES.keys()))
    units = SelectField(label="Units", choices=list(LS372_INPUT_SENSOR_UNITS.keys()))
    resistance_range = SelectField(label="Resistance Range", choices=list(LS372_RESISTANCE_RANGE.keys()))
    dwell_time = IntegerField(label="Dwell Time", default=1, validators=[NumberRange(1, 200)])
    pause_time = IntegerField(label="Pause Time", default=3, validators=[NumberRange(3, 200)])
    temperature_coefficient = SelectField(label="Temperature Coefficient", choices=list(LS372_CURVE_COEFFICIENTS))
    enable = SelectField(label="Enable", choices=list(LS372_ENABLED_VALUES.keys()))
    update = SubmitField("Update")


class OutputHeaterForm(FlaskForm):
    from mkidcontrol.commands import LS372_HEATER_OUTPUT_MODE, LS372_HEATER_INPUT_CHANNEL, \
        LS372_HEATER_POWERUP_ENABLE, LS372_HEATER_READING_FILTER, LS372_OUTPUT_POLARITY, LS372_HEATER_CURRENT_RANGE

    channel = HiddenField("")
    name = StringField("Name")
    output_mode = SelectField("Output Mode", choices=list(LS372_HEATER_OUTPUT_MODE.keys()))
    input_channel = SelectField("Input Channel", choices=list(LS372_HEATER_INPUT_CHANNEL.keys()))
    powerup_enable = SelectField("Powerup Enable", choices=list(LS372_HEATER_POWERUP_ENABLE.keys()))
    reading_filter = SelectField("Autorange Enable", choices=list(LS372_HEATER_READING_FILTER.keys()))
    delay = IntegerField("Delay", default=1, validators=[NumberRange(1, 255)])
    polarity = SelectField("Polarity", choices=list(LS372_OUTPUT_POLARITY.keys()))
    setpoint = FloatField("Temperature Setpoint (K)", default=0.100,  validators=[NumberRange(0,300)])
    gain = FloatField("PID Gain (P)", default=0,  validators=[NumberRange()])  # TODO: Find values allowable
    integral = FloatField("PID Integral (I)", default=0,  validators=[NumberRange()])  # TODO: Find values allowable
    ramp_rate = FloatField("PID Ramp Rate (D)", default=0,  validators=[NumberRange()])  # TODO: Find values allowable
    range = SelectField("Range", choices=list(LS372_HEATER_CURRENT_RANGE))
    update = SubmitField("Update")


class DisabledOutputHeaterForm(FlaskForm):
    from mkidcontrol.commands import LS372_HEATER_OUTPUT_MODE, LS372_HEATER_INPUT_CHANNEL, \
        LS372_HEATER_POWERUP_ENABLE, LS372_HEATER_READING_FILTER, LS372_OUTPUT_POLARITY, LS372_HEATER_CURRENT_RANGE
    channel = HiddenField("")
    name = StringField("Name")
    output_modemode = SelectField("Output Mode", choices=list(LS372_HEATER_OUTPUT_MODE.keys()))
    input_channel = SelectField("Input Channel", choices=list(LS372_HEATER_INPUT_CHANNEL.keys()), render_kw={'disabled': True})
    powerup_enable = SelectField("Powerup Enable", choices=list(LS372_HEATER_POWERUP_ENABLE.keys()), render_kw={'disabled': True})
    reading_filter = SelectField("Autorange Enable", choices=list(LS372_HEATER_READING_FILTER.keys()), render_kw={'disabled': True})
    delay = IntegerField("Delay", default=1, validators=[NumberRange(1, 255)], render_kw={'disabled': True})
    polarity = SelectField("Polarity", choices=list(LS372_OUTPUT_POLARITY.keys()), render_kw={'disabled': True})
    setpoint = FloatField("Temperature Setpoint (K)", default=0.100, validators=[NumberRange(0, 300)], render_kw={'disabled': True})
    gain = FloatField("PID Gain (P)", default=0, validators=[NumberRange()], render_kw={'disabled': True})  # TODO: Find values allowable
    integral = FloatField("PID Integral (I)", default=0, validators=[NumberRange()], render_kw={'disabled': True})  # TODO: Find values allowable
    ramp_rate = FloatField("PID Ramp Rate (D)", default=0, validators=[NumberRange()], render_kw={'disabled': True})  # TODO: Find values allowable
    range = SelectField("Range", choices=list(LS372_HEATER_CURRENT_RANGE), render_kw={'disabled': True})
    update = SubmitField("Update")


if __name__ == "__main__":

    util.setup_logging('lakeshore372Agent')
    redis.setup_redis(ts_keys=TS_KEYS)

    try:
        log.debug(f"Connecting to LakeShore 372...")
        try:
            lakeshore = LakeShore372('LakeShore372', baudrate=57600, port='/dev/ls372',
                                     enabled_input_channels=ENABLED_372_INPUT_CHANNELS)#, initializer=initializer)
            log.info(f"LakeShore 372 connection successful!")
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
                        # TODO: May need to publish this as well, need to walk through what other programs need this value
                        redis.store({REGULATION_TEMP_KEY: cmd.command_value})
                    redis.store({STATUS_KEY: "OK"})
                except IOError as e:
                    redis.store({STATUS_KEY: f"Error"})
                    log.error(f"Comm error: {e}")
    except RedisError as e:
        log.critical(f"Redis server error! {e}")
        sys.exit(1)
