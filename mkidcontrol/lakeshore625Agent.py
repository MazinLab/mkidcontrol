"""
Author: Noah Swimmer
21 February 2022

Program for communicating with and controlling the LakeShore625 Superconducting Magnet Power Supply.

This module is responsible for TODO

TODO: Quench detection values. Theory for  value choice exists at npage 48 of the LakeShore 625 manual
 (V_LS625compliance,max = 5V, V_max,magnet=125 mV,  L=~35H, I_max=9.4A)

TODO: All
"""

import sys
import time
import logging
import threading
from datetime import datetime, timedelta
from collections import defaultdict
from transitions import MachineError, State, Transition
from transitions.extensions import LockedMachine
import pkg_resources

from mkidcontrol.devices import LakeShore625, MagnetState
from mkidcontrol.mkidredis import RedisError
import mkidcontrol.util as util
import mkidcontrol.mkidredis as redis
from mkidcontrol.commands import COMMANDS625, LakeShoreCommand
import mkidcontrol.heatswitchAgent as heatswitch
import mkidcontrol.lakeshore372Agent as ls372

QUERY_INTERVAL = 1
MAX_PERSISTED_STATE_LIFE_SECONDS = 3600

DEVICE = '/dev/ls625'
VALID_MODELS = ('MODEL625', )

SOAK_TIME_KEY = 'device-settings:ls625:soak-time'
SOAK_CURRENT_KEY = 'device-settings:ls625:soak-current'
RAMP_RATE_KEY = 'device-settings:ls625:ramp-rate'
COOLDOWN_SCHEDULED_KEY = 'device-settings:ls625:cooldown-scheduled'

IMPOSE_UPPER_LIMIT_ON_REGULATION_KEY = 'device-settings:ls625:enable-temperature-regulation-upper-limit'
STATEFILE_PATH_KEY = 'device-settings:ls625:statefile'  # /mkidcontrol/mkidcontrol/logs/statefile.txt

STOP_RAMP_KEY = 'command:device-settings:ls625:stop-current-ramp'
COLD_AT_CMD = 'be-cold-at'
COLD_NOW_CMD = 'get-cold'
ABORT_CMD = 'abort-cooldown'
CANCEL_COOLDOWN_CMD = 'cancel-scheduled-cooldown'
QUENCH_KEY = 'event:quenching'

MAGNET_COMMAND_KEYS = (COLD_AT_CMD, COLD_NOW_CMD, ABORT_CMD, CANCEL_COOLDOWN_CMD, STOP_RAMP_KEY)

SETTING_KEYS = tuple(COMMANDS625.keys())

STATUS_KEY = "status:device:ls625:status"
FIRMWARE_KEY = "status:device:ls625:firmware"
MODEL_KEY = 'status:device:ls625:model'
SN_KEY = 'status:device:ls625:sn'

MAGNET_STATE_KEY = 'status:magnet:state' # OFF | RAMPING | SOAKING | QUENCH (DON'T QUENCH!)
MAGNET_CURRENT_KEY = 'status:magnet:current'
MAGNET_FIELD_KEY = 'status:magnet:field'
OUTPUT_VOLTAGE_KEY = 'status:device:ls625:output-voltage'

TS_KEYS = [MAGNET_CURRENT_KEY, MAGNET_FIELD_KEY, OUTPUT_VOLTAGE_KEY]

COMMAND_KEYS = [f"command:{k}" for k in SETTING_KEYS + MAGNET_COMMAND_KEYS]

DEVICE_TEMP_KEY = 'status:temps:device-stage:temp'
REGULATION_TEMP_KEY = "device-settings:device-stage:regulating-temp"

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


class LakeShore625(LakeShoreDevice):
    def __init__(self, name, port, baudrate=9600, parity=serial.PARITY_ODD, bytesize=serial.SEVENBITS, timeout=0.1, connect=True, valid_models=None, initializer=None):
        super().__init__(name, port, baudrate=baudrate, timeout=timeout, parity=parity, bytesize=bytesize,
                         connect=connect, valid_models=valid_models, initializer=initializer)

        if connect:
            self.connect(raise_errors=False)

        self.last_current_read = None
        self.last_field_read = None

    def current(self):
        current = self.query("RDGI?")
        self.last_current_read = current
        return current

    def field(self):
        field = self.query("RDGF?")
        self.last_field_read = field
        return field

    def output_voltage(self):
        voltage = self.query("RDGV?")
        self.last_voltage_read = voltage
        return voltage

    def kill_current(self):
        # TODO
        pass

if __name__ == "__main__":

    util.setup_logging('lakeshore625Agent')
    redis.setup_redis(create_ts_keys=TS_KEYS)
    lakeshore = LakeShore625(port=DEVICE, valid_models=VALID_MODELS, initializer=initializer)

    # ---------------------------------- MAIN OPERATION (The eternal loop) BELOW HERE ----------------------------------
    def monitor_callback(I, F, ov):
        d = {k: x for k, x in zip((MAGNET_CURRENT_KEY, MAGNET_FIELD_KEY, OUTPUT_VOLTAGE_KEY), (I, F, ov)) if x}
        redis.store(d, timeseries=True)

    lakeshore.monitor(QUERY_INTERVAL, (lakeshore.current, lakeshore.field, lakeshore.output_voltage), value_callback=monitor_callback)