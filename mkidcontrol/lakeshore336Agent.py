"""
Author: Noah Swimmer
21 February 2022

Program for communicating with and controlling the LakeShore336 Cryogenic Temperature Controller.

This module is responsible for reading out the non-device thermometry. This includes the intermediate stages (77K and
4K) and the 1K stage

N.B. Python API at https://lake-shore-python-driver.readthedocs.io/en/latest/model_336.html

TODO: Command syntax

TODO: Error handling

TODO: Enable/disable channels

TODO: Logging

TODO: Docstrings
"""

import sys
import time
import logging
import threading
import numpy as np
from serial.serialutil import SerialException

from mkidcontrol.mkidredis import MKIDRedis, RedisError
from mkidcontrol.devices import LakeShoreMixin
import mkidcontrol.util as util

from lakeshore import Model336,  Model336CurveHeader, Model336CurveFormat, Model336CurveTemperatureCoefficients, \
                      Model336InputSensorUnits, Model336InputSensorSettings, Model336InputSensorType, \
                      Model336RTDRange, Model336DiodeRange, Model336ThermocoupleRange

TEMP_KEYS = ['status:temps:77k-stage:temp', 'status:temps:4k-stage:temp', 'status:temps:1k-stage:temp']
SENSOR_VALUE_KEYS = ['status:temps:77k-stage:resistance', 'status:temps:4k-stage:voltage', 'status:temps:1k-stage:voltage']

TS_KEYS = TEMP_KEYS + SENSOR_VALUE_KEYS

FIRMWARE_KEY = "status:device:ls336:firmware"
MODEL_KEY = 'status:device:ls336:model'
SN_KEY = 'status:device:ls336:sn'

QUERY_INTERVAL = 1

log = logging.getLogger(__name__)
log.setLevel("DEBUG")

ENABLED_CHANNELS = ['B', 'C', 'D']
ALLOWED_CHANNELS = ['A', 'B', 'C', 'D']

COMMAND_KEYS = ['command:device-settings:ls336:channel-b:curve']


class LakeShore336(LakeShoreMixin, Model336):
    def __init__(self, name, port=None, timeout=0.1):
        self.device_serial = None
        self.enabled_input_channels = ENABLED_CHANNELS

        if port is None:
            super().__init__(timeout=timeout)
        else:
            super().__init__(com_port=port, timeout=timeout)
        self.name = name

    def sensor_settings(self, channel):
        """
        Returns a parsed dictionary
        """
        try:
            sensor_data = vars(self.get_input_sensor(str(channel)))
            log.debug(f"Read input sensor data for channel {channel}: {sensor_data}")
            return sensor_data
        except IOError as e:
            raise IOError(f"Serial error communicating with Lake Shore 336: {e}")
        except ValueError as e:
            raise ValueError(f"{channel} is not an allowed channel for the Lake Shore 336: {e}")

    def modify_input_sensor(self, channel, sensor_type=None, autorange_enable=None,
                            compensation=None, units=None, input_range=None):

        settings = self.sensor_settings(channel)

        desired_settings = {'sensor_type': sensor_type, 'autorange_enable': autorange_enable,
                        'compensation': compensation, 'units': units,'input_range': input_range}

        new_settings = self._modify_settings_dict(settings, desired_settings)

        if new_settings['sensor_type'] == 0:
            new_settings['input_range'] = None
        elif new_settings['sensor_type'] == 1:
            new_settings['input_range'] = Model336DiodeRange(new_settings['input_range'])
        elif new_settings['sensor_type'] in (2, 3):
            new_settings['input_range'] = Model336RTDRange(new_settings['input_range'])
        elif new_settings['sensor_type'] == 4:
            new_settings['input_range'] = Model336ThermocoupleRange(new_settings['input_range'])
        else:
            raise ValueError(f"{new_settings['sensor_type']} is not an allowed value!")

        settings = Model336InputSensorSettings(sensor_type=Model336InputSensorType(new_settings['sensor_type']),
                                               autorange_enable=new_settings['autorange_enable'],
                                               compensation=new_settings['compensation'],
                                               units=Model336InputSensorUnits(new_settings['units']),
                                               input_range=new_settings['input_range'])
        return settings
        # self.set_input_sensor(channel=channel_num, sensor_parameters=settings)

    def modify_curve_header(self, curve_num, curve_name=None, serial_number=None, curve_data_format=None,
                            temperature_limit=None, coefficient=None):

        settings = self.curve_settings(curve_num)

        desired_settings = {'curve_name': curve_name, 'serial_number': serial_number,
                            'curve_data_format': curve_data_format, 'temperature_limit': temperature_limit,
                            'coefficient': coefficient}

        new_settings = self._modify_settings_dict(settings, desired_settings)

        header = Model336CurveHeader(curve_name=new_settings['curve_name'],
                                     serial_number=new_settings['serial_number'],
                                     curve_data_format=Model336CurveFormat(new_settings['curve_data_format']),
                                     temperature_limit=new_settings['temperature_limit'],
                                     coefficient=Model336CurveTemperatureCoefficients(new_settings['coefficient']))

        return header
        # self.set_curve_header(curve_number=curve_num, curve_header=header)

if __name__ == "__main__":

    util.setup_logging('lakeshore336Agent')
    redis = MKIDRedis(create_ts_keys=TS_KEYS)

    try:
        lakeshore = LakeShore336('LakeShore336', '/dev/ls336')
    except:
        lakeshore = LakeShore336('LakeShore336')

    try:
        info = lakeshore.device_info
        # Note that placing the store before exit makes this program behave differently in an abort
        #  than both of the sims, which would not alter the database. I like this better.
        redis.store({FIRMWARE_KEY: info['firmware'], MODEL_KEY: info['model'], SN_KEY: info['sn']})
    except IOError as e:
        log.error(f"When checking device info: {e}")
        redis.store({FIRMWARE_KEY: '', MODEL_KEY: '', SN_KEY: ''})
        sys.exit(1)
    except RedisError as e:
        log.critical(f"Redis server error! {e}")
        sys.exit(1)

    def callback(vals):
        keys = TEMP_KEYS + SENSOR_VALUE_KEYS
        d = {k: x for k, x in zip(keys, vals) if x}
        redis.store(d, timeseries=True)

    lakeshore.monitor(QUERY_INTERVAL, lakeshore.read_temperatures_and_sensor_values, value_callback=callback)

    try:
        while True:
            for key, val in redis.listen(COMMAND_KEYS):
                log.info(f"heard {key} -> {val}!")
                # TODO: Command handling
    except RedisError as e:
        log.error(f"Redis server error! {e}")

    print('go')
