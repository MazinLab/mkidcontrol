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

from lakeshore import Model372, Model336, Model372CurveHeader, Model372CurveFormat, Model372CurveTemperatureCoefficient, \
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

COMMANDS372 = {'device-settings:ls372:input-channel-a:': {'command': '', 'vals': ''}}


class LakeShore372(LakeShoreMixin, Model372):
    def __init__(self, name, baudrate=57600, port=None, timeout=0.1):

        self.enabled_input_channels = ENABLED_INPUT_CHANNELS
        if port is None:
            super().__init__(baud_rate=baudrate, timeout=timeout)
        else:
            super().__init__(baud_rate=baudrate, com_port=port, timeout=timeout)
        self.name = name

    @property
    def setpoint(self):
        return self.get_setpoint_kelvin(0)

    def configure_input_sensor(self, channel_num, command_code, **desired_settings):
        settings = self.query_settings(command_code, channel_num)

        new_settings = {}
        for k in settings.keys():
            try:
                new_settings[k] = desired_settings[k]
            except KeyError:
                new_settings[k] = settings[k]

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

    def modify_channel_settings(self, channel, command_code, **desired_settings):
        settings = self.query_settings(command_code, channel)

        new_settings = {}
        for k in settings.keys():
            try:
                new_settings[k] = desired_settings[k]
            except KeyError:
                new_settings[k] = settings[k]

        settings = Model372InputChannelSettings(enable=new_settings['enable'],
                                                dwell_time=new_settings['dwell_time'],
                                                pause_time=new_settings['pause_time'],
                                                curve_number=new_settings['curve_number'],
                                                temperature_coefficient=Model372CurveTemperatureCoefficient(new_settings['temperature_coefficient']))

        return settings
        # self.set_input_channel_parameters(channel, settings)

    def configure_heater_settings(self, heater_channel, command_code, **desired_settings):

        settings = self.query_settings(command_code, heater_channel)

        new_settings = {}
        for k in settings.keys():
            try:
                new_settings[k] = desired_settings[k]
            except KeyError:
                new_settings[k] = settings[k]

        settings = Model372HeaterOutputSettings(output_mode=Model372OutputMode(new_settings['output_mode']),
                                                input_channel=Model372InputChannel(new_settings['input_channel']),
                                                powerup_enable=new_settings['powerup_enable'],
                                                reading_filter=new_settings['reading_filter'],
                                                delay=new_settings['delay'],
                                                polarity=Model372Polarity(new_settings['polarity']))

        return settings
        # self.configure_heater(output_channel=heater_channel, settings=settings)

    def change_temperature_setpoint(self, channel, command_code, setpoint=None):
        current_setpoint = self.query_settings(command_code, channel)
        if current_setpoint != setpoint and setpoint is not None:
            log.info(f"Changing temperature regulation value for output channel {channel} to {setpoint} from "
                     f"{current_setpoint}")
            self.set_setpoint_kelvin(output_channel=channel, setpoint=setpoint)
        else:
            log.info(f"Requested to set temperature setpoint from {current_setpoint} to {setpoint}, no change"
                        f"sent to Lake Shore 372.")

    def modify_pid_settings(self, output_channel, command_code, **desired_settings):
        settings = self.query_settings(command_code, output_channel)

        new_settings = {}
        for k in settings.keys():
            try:
                new_settings[k] = desired_settings[k]
            except KeyError:
                new_settings[k] = settings[k]

        return new_settings
        # self.set_heater_pid(output_channel, gain=new_settings['gain'], integral=new_settings['integral'],
        #                     derivative=new_settings['ramp_rate'])

    def modify_heater_output_range(self, output_channel, command_code, range=None):
        current_range = self.query_settings(command_code, output_channel)

        # TODO:
        if output_channel == 0:
            if current_range.value == range or range is None:
                log.info(f"Attempting to set the output range for the output heater from {current_range.name} to the "
                         f"same value. No change requested to the instrument.")
            else:
                self.set_heater_output_range(output_channel, Model372SampleHeaterOutputRange(range))
        else:
            if current_range == range or range is None:
                log.info(f"Attempting to set the output range for the output heater from {current_range} to the "
                         f"same value. No change requested to the instrument.")
            else:
                self.set_heater_output_range(output_channel, range)


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
