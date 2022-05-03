"""
Author: Noah Swimmer
21 February 2022

Program for communicating with and controlling the LakeShore372 AC Resistance Bridge.

This module is responsible for reading out the device temperature and providing the signal in the PID loop which will
ultimately control the thermometer. The signal will be sent to the Lake Shore 625 magnet controller for producing a
control current in the magnet.

From the manual -> Do not query more than 20 times per second

*NOTE: For queries where you access a specific channel (e.g. Output A,1,2, Input A,1-16, etc.) if you go above the legal
values (i.e. Output 7) then it will return the values for the highest number channel. (For example, querying OUTMODE? 5
will give you the reult of OUTMODE? 2, since 2 is the highest channel number available to that query)

N.B. Python API at https://lake-shore-python-driver.readthedocs.io/en/latest/model_372.html

TODO: Error handling

TODO: Enable/disable channels

TODO: Logging

TODO: Docstrings

TODO: Monitor Output

TODO: Command syntax
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

from lakeshore import Model372, Model372CurveHeader, Model372CurveFormat, Model372CurveTemperatureCoefficient, \
                      Model372SensorExcitationMode, Model372MeasurementInputCurrentRange, Model372AutoRangeMode, \
                      Model372InputSensorUnits, Model372MeasurementInputResistance, Model372HeaterOutputSettings, \
                      Model372OutputMode, Model372InputChannel, Model372InputSetupSettings, \
                      Model372ControlInputCurrentRange, Model372MeasurementInputVoltageRange, \
                      Model372InputChannelSettings, Model372Polarity, Model372SampleHeaterOutputRange

TS_KEYS = ['status:temps:lhetank', 'status:temps:ln2tank']

FIRMWARE_KEY = "status:device:ls372:firmware"
MODEL_KEY = 'status:device:ls372:model'
SN_KEY = 'status:device:ls372:sn'

QUERY_INTERVAL = 1
VALID_MODELS = ('MODEL372', )

log = logging.getLogger()
log.setLevel("DEBUG")

ENABLED_INPUT_CHANNELS = ["A", "1"]
ENABLED_OUTPUT_CHANNELS = [0]

COMMANDS372 = {'device-settings:ls372:': {'command': '', 'vals': ''}}


class LakeShore372(LakeShoreMixin, Model372):
    def __init__(self, name, baudrate=57600, port=None, timeout=0.1):
        self.device_serial = None
        self.enabled_input_channels = ENABLED_INPUT_CHANNELS
        if port is None:
            super().__init__(baud_rate=baudrate, timeout=timeout)
        else:
            super().__init__(baud_rate=baudrate, com_port=port, timeout=timeout)
        self.name = name

    def excitation_power(self):
        readings = []
        for channel in ENABLED_INPUT_CHANNELS:
            try:
                readings.append(float(self.get_excitation_power(channel)))
            except IOError as e:
                log.error(f"Serial error: {e}")
                raise IOError(f"Serial error: {e}")

        if len(ENABLED_INPUT_CHANNELS) == 1:
            readings = readings[0]

        return readings

    def input_sensor_settings(self, channel):
        try:
            sensor_data = vars(self.get_input_setup_parameters(str(channel)))
            log.debug(f"Reading input setup settings from channel {channel}: {sensor_data}")
            return sensor_data
        except IOError as e:
            raise IOError(f"Serial error communicating with Lake Shore 372: {e}")
        except ValueError as e:
            raise ValueError(f"{channel} is not an allowed channel for the Lake Shore 336: {e}")

    def configure_input_sensor(self, channel_num, mode=None, excitation_range=None, auto_range=None,
                               current_source_shunted=None, units=None, resistance_range=None):

        settings = self.input_sensor_settings(channel_num)

        desired_settings = {'mode': mode, 'excitation_range': excitation_range, 'auto_range': auto_range,
                        'current_source_shunted': current_source_shunted, 'units': units, 'resistance_range': resistance_range}

        new_settings = self._modify_settings_dict(settings, desired_settings)

        if channel_num == "A":
            new_settings['excitation_range'] = Model372ControlInputCurrentRange(new_settings['excitation_range'])
            new_settings['resistance_range'] = None
        else:
            if new_settings['mode'] == 0:
                new_settings['excitation_range'] = Model372MeasurementInputVoltageRange(new_settings['excitation_range'])
            elif new_settings['mode'] == 1:
                new_settings['excitation_range'] = Model372MeasurementInputCurrentRange(new_settings['excitation_range'])
            else:
                raise ValueError(f"{new_settings['mode']} is not an allowed value!")

        settings = Model372InputSetupSettings(mode=Model372SensorExcitationMode(new_settings['mode']),
                                              excitation_range=new_settings['excitation_range'],
                                              auto_range=Model372AutoRangeMode(new_settings['auto_range']),
                                              current_source_shunted=new_settings['current_source_shunted'],
                                              units=Model372InputSensorUnits(new_settings['units']),
                                              resistance_range=Model372MeasurementInputResistance(new_settings['resistance_range']))

        return settings
        # self.configure_input(input_channel=channel_num, settings=settings)

    def input_channel_settings(self, channel):
        try:
            data = vars(self.get_input_channel_parameters(channel))
            log.debug(f"Reading parameters for input channel {channel}: {data}")
            return data
        except IOError as e:
            raise IOError(f"Serial error communicating with Lake Shore 372: {e}")
        except ValueError as e:
            raise ValueError(f"{channel} is not an allowed channel for the Lake Shore 336: {e}")

    def modify_channel_settings(self, channel, enable=None, dwell_time=None, pause_time=None,
                                curve_number=None, temperature_coefficient=None):

        settings = self.input_channel_settings(channel)

        desired_settings = {'enable': enable, 'dwell_time': dwell_time, 'pause_time': pause_time,
                        'curve_number': curve_number, 'temperature_coefficient': temperature_coefficient}

        new_settings = self._modify_settings_dict(settings, desired_settings)

        settings = Model372InputChannelSettings(enable=new_settings['enable'],
                                                dwell_time=new_settings['dwell_time'],
                                                pause_time=new_settings['pause_time'],
                                                curve_number=new_settings['curve_number'],
                                                temperature_coefficient=Model372CurveTemperatureCoefficient(new_settings['temperature_coefficient']))

        return settings
        # self.set_input_channel_parameters(channel, settings)

    def heater_settings(self, heater_channel):
        try:
            sensor_data = vars(self.get_heater_output_settings(heater_channel))
            log.debug(f"Read heater settings for heater channel {heater_channel}: {sensor_data}")
            return sensor_data
        except IOError as e:
            raise IOError(f"Serial error communicating with Lake Shore 372: {e}")
        except ValueError as e:
            raise ValueError(f"{heater_channel} is not an allowed channel for the Lake Shore 336: {e}")

    def configure_heater_settings(self, heater_channel, output_mode=None, input_channel=None, powerup_enable=None,
                                  reading_filter=None, delay=None, polarity=None):

        settings = self.heater_settings(heater_channel)

        desired_settings = {'output_mode': output_mode, 'input_channel': input_channel, 'powerup_enable': powerup_enable,
                        'polarity': polarity, 'reading_filter': reading_filter, 'delay': delay}

        # TODO: Turn this loop into a function
        new_settings = self._modify_settings_dict(settings, desired_settings)

        settings = Model372HeaterOutputSettings(output_mode=Model372OutputMode(new_settings['output_mode']),
                                                input_channel=Model372InputChannel(new_settings['input_channel']),
                                                powerup_enable=new_settings['powerup_enable'],
                                                reading_filter=new_settings['reading_filter'],
                                                delay=new_settings['delay'],
                                                polarity=Model372Polarity(new_settings['polarity']))

        return settings
        # self.configure_heater(output_channel=heater_channel, settings=settings)

    def change_temperature_setpoint(self, channel=0, setpoint=0.100):
        current_setpoint = self.get_setpoint_kelvin(channel)
        if current_setpoint != setpoint:
            log.info(f"Changing temperature regulation value for output channel {channel} to {setpoint} from "
                     f"{current_setpoint}")
            self.set_setpoint_kelvin(output_channel=channel, setpoint=setpoint)
        else:
            log.info(f"Requested to set temperature setpoint from {current_setpoint} to {setpoint}, no change"
                        f"sent to Lake Shore 372.")

    @property
    def setpoint(self):
        return self.get_setpoint_kelvin(0)

    def read_pid_settings(self, output_channel):
        return self.get_heater_pid(output_channel)

    def modify_pid_settings(self, output_channel, p=None, i=None, d=None):
        settings = self.read_pid_settings(output_channel)

        desired_settings = {'gain': p, 'integral': i, 'ramp_rate': d}

        new_settings = self._modify_settings_dict(settings, desired_settings)

        return new_settings
        # self.set_heater_pid(output_channel, gain=new_settings['gain'], integral=new_settings['integral'],
        #                     derivative=new_settings['ramp_rate'])

    def modify_heater_output_range(self, output_channel, range):
        current_range = self.get_heater_output_range(output_channel)

        if output_channel == 0:
            if current_range.value == range:
                log.info(f"Attempting to set the output range for the output heater from {current_range.name} to the "
                         f"same value. No change requested to the instrument.")
            else:
                self.set_heater_output_range(output_channel, Model372SampleHeaterOutputRange(range))
        else:
            if current_range == range:
                log.info(f"Attempting to set the output range for the output heater from {current_range} to the "
                         f"same value. No change requested to the instrument.")
            else:
                self.set_heater_output_range(output_channel, range)

    def modify_curve_header(self, curve_num, curve_name=None, serial_number=None, curve_data_format=None,
                            temperature_limit=None, coefficient=None):

        settings = self.curve_settings(curve_num)

        desired_settings = {'curve_name': curve_name, 'serial_number': serial_number,
                            'curve_data_format': curve_data_format, 'temperature_limit': temperature_limit,
                            'coefficient': coefficient}

        new_settings = self._modify_settings_dict(settings, desired_settings)

        header = Model372CurveHeader(curve_name=new_settings['curve_name'],
                                     serial_number=new_settings['serial_number'],
                                     curve_data_format=Model372CurveFormat(new_settings['curve_data_format']),
                                     temperature_limit=new_settings['temperature_limit'],
                                     coefficient=Model372CurveTemperatureCoefficient(new_settings['coefficient']))

        return header
        # self.set_curve_header(curve_number=curve_num, curve_header=header)


if __name__ == "__main__":

    util.setup_logging('lakeshore372Agent')  # TODO: Add to logging yaml
    redis = MKIDRedis(create_ts_keys=TS_KEYS)

    try:
        lakeshore = LakeShore372('LakeShore372', 57600, '/dev/ls372')
    except:
        lakeshore = LakeShore372('LakeShore372', 57600)

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

    # TODO: Main loop with callback/monitor architecture (see sim921agent for example)
    def callback(keys, ENABLED_INPUT_CHANNELS):
        pass
    # lakeshore.monitor(QUERY_INTERVAL, lakeshore.read_temperatures(), value_callback=callback)

    # while True:
    #     try:
    #         temps = lakeshore.read_temperatures()
    #         # redis.store({'status:temps:thermometer-b': temps["B"],
    #         #              'status:temps:thermometer-c': temps["C"],
    #         #              'status:temps:thermometer-d': temps["D"]}, timeseries=True)
    #     except (IOError, ValueError) as e:
    #         log.error(f"Communication with LakeShore 336 failed: {e}")
    #     except RedisError as e:
    #         log.critical(f"Redis server error! {e}")
    #         sys.exit(1)
    #     time.sleep(QUERY_INTERVAL)

    # TODO: Listen for settings changes / handle changes

    print('go')
