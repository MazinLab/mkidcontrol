"""
Author: Noah Swimmer
21 February 2022

Program for communicating with and controlling the LakeShore625 Superconducting Magnet Power Supply which is a current
source for the magnet.

We have documented this in as many places as possible, but the 'limits' one may set for the magnet are (1) output
current, (2) compliance voltage, and (3) ramp rate limit. All are set by parameters of the magnet and so will be
different for different ADRs (luckily we use very similar ones so between DARKNESS/PICTUREC/ARCONS/XKID, the same values
should mostly work, although DO be careful to ensure that BEFORE you run the magnet).

N.B. (19 July 2022): The limiting values are as follows (and remember, the limits for everything except current should
 be a few percent higher than the expected value)
- Output current: 9.44A
- Compliance voltage: 1.75 V & 5 mA/s (This is NOT equivalent to the maximum back EMF, see the doc on 'lakeshore 625
   magnet limits' on the mazinlab wiki)
   - To calculate: V_compliance_minimum = L_magnet * (di/dt)_desired + I_max * R_system)
                                        = 35 H * 5 mA/s + 9.44 A * 0.16 Ohm
                   R_system was measured by N.S. simply using a multimeter between the + and - terminals of the magnet cable
                   (di/dt)_desired is tunable, we don't recommend higher than 10 mA/s
- Ramp rate: 20 mA/s (you want this to be about double + 10% of the max rate you expect to use)

# TODO: Add 'pause current' command
# TODO: Clean code
"""

import sys
import time
import logging
from mkidcontrol.devices import LakeShore625, MagnetState, write_persisted_state, load_persisted_state
from mkidcontrol.mkidredis import RedisError
import mkidcontrol.util as util
import mkidcontrol.mkidredis as redis
from mkidcontrol.commands import COMMANDS625, LakeShoreCommand

log = logging.getLogger()

QUERY_INTERVAL = 1

SETTING_KEYS = tuple(COMMANDS625.keys())

DEVICE = '/dev/ls625'
VALID_MODELS = ('MODEL625', )

DESIRED_CURRENT_KEY = 'device-settings:ls625:desired-current'

STATUS_KEY = "status:device:ls625:status"
FIRMWARE_KEY = "status:device:ls625:firmware"
MODEL_KEY = 'status:device:ls625:model'
SN_KEY = 'status:device:ls625:sn'

MAGNET_CURRENT_KEY = 'status:magnet:current'
MAGNET_FIELD_KEY = 'status:magnet:field'
OUTPUT_VOLTAGE_KEY = 'status:device:ls625:output-voltage'

TS_KEYS = [MAGNET_CURRENT_KEY, MAGNET_FIELD_KEY, OUTPUT_VOLTAGE_KEY]

COMMAND_KEYS = [f"command:{k}" for k in SETTING_KEYS]


import wtforms
from wtforms.fields import *
from wtforms.widgets import HiddenInput
from wtforms.fields.html5 import *
from wtforms.validators import *
from wtforms import Form
from flask_wtf import FlaskForm
from serial import SerialException


class Lakeshore625ControlForm(FlaskForm):
    desired_current = FloatField("Set Current (Manual)", default=0, validators=[NumberRange(0, 7.88)])
    magnetic_field_parameter = FloatField("Magnetic Field Parameter (kG/A)", default=4.0609, validators=[NumberRange(0, 10)])
    ramp_rate = FloatField("Ramp Rate (A/s)", default=0.005, validators=[NumberRange(0, 0.020)])
    compliance_voltage = FloatField("Compliance Voltage", default=2.5, validators=[NumberRange(0.1, 5.0)])
    control_mode = SelectField("Control Mode", choices=list(COMMANDS625['device-settings:ls625:control-mode']['vals'].keys()))
    quench_ramp_rate = FloatField("Quench Ramp Rate (A/s)", default=0.020, validators=[NumberRange(0.01, 10)])
    baud_rate = SelectField("Baud Rate", choices=list(COMMANDS625['device-settings:ls625:baud-rate']['vals'].keys()), render_kw={'disabled': True})
    current_limit = FloatField("Current Limit", default=7.88, validators=[NumberRange(0, 10.0)])
    compliance_voltage_limit = FloatField("Comliance Voltage Limit", default=2.5, validators=[NumberRange(0.1, 5.0)])
    rate_limit = FloatField("Ramp Rate Limit (A/s)", default=0.015, validators=[NumberRange(0, 0.050)])
    update = SubmitField("Update")


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


def callback(cur, field, ov):
    d = {k: float(x) for k, x in zip((MAGNET_CURRENT_KEY, MAGNET_FIELD_KEY, OUTPUT_VOLTAGE_KEY), (cur, field, ov)) if
         x}
    try:
        if all(i is None for i in [cur, field, ov]) is None:
            # N.B. If there is an error on the query, the value passed is None
            redis.store({STATUS_KEY: "Error"})
        else:
            redis.store(d, timeseries=True)
            redis.store({STATUS_KEY: "OK"})
    except RedisError:
        log.warning('Storing LakeShore625 data to redis failed!')


if __name__ == "__main__":

    util.setup_logging('lakeshore625Agent')
    redis.setup_redis(ts_keys=TS_KEYS)

    try:
        log.debug(f"Connecting to LakeShore 625")
        lakeshore = LakeShore625(port=DEVICE, valid_models=VALID_MODELS, initializer=initializer)
        log.info(f"LakeShore 625 connection successful!")
        redis.store({STATUS_KEY: "OK"})
    except IOError as e:
        log.critical(f"Error in connecting to LakeShore 625: {e}")
        redis.store({STATUS_KEY: "Error"})
        sys.exit(1)
    except RedisError as e:
        log.critical(f"Error in communicating with redis: {e}")
        sys.exit(1)

    lakeshore.monitor(QUERY_INTERVAL, (lakeshore.current, lakeshore.field, lakeshore.output_voltage),
                      value_callback=callback)

    # main loop, listen for commands and handle them
    try:
        while True:
            for key, val in redis.listen(COMMAND_KEYS):
                log.debug(f"lakeshore625agent received {key}, {val}. Trying to send a command")
                key = key.removeprefix('command:')
                # TODO: Handle STOP ramp
                if key in SETTING_KEYS:
                    try:
                        limits = lakeshore.limits  # N.B. This is a fast call and if the command needs it it will have it, otherwise it will be ignored
                        cmd = LakeShoreCommand(key, val, limit_vals=limits)
                    except ValueError:
                        log.warning(f"Ignoring invalid command ('{key}={val}'): {e}")
                        continue
                    except IOError as e:
                        log.error(f"Comm error: {e}")
                        redis.store({STATUS_KEY: f"Error {e}"})
                    try:
                        log.info(f"Processing command '{cmd}'")
                        lakeshore.send(cmd.ls_string)
                        redis.store({cmd.setting: cmd.value})
                        redis.store({STATUS_KEY: "OK"})
                    except IOError as e:
                        redis.store({STATUS_KEY: f"Error {e}"})
                        log.error(f"Comm error: {e}")
    except RedisError as e:
        log.critical(f"Redis server error! {e}", exc_info=True)
        sys.exit(1)
