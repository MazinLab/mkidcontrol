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

TODO: Enable/disable channels

TODO: Docstrings

TODO: 'Block' settings (e.g. excitation cannot be in V if mode is Current)
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

log = logging.getLogger()

ENABLED_INPUT_CHANNELS = ("A")
ALLOWED_INPUT_CHANNELS = ("A", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12", "13", "14", "15", "16")
ENABLED_OUTPUT_CHANNELS = (0, )
ALLOWED_OUTPUT_CHANNELS = (0, 1, 2)

COMMANDS372 = {}
COMMANDS372.update({f'device-settings:ls372:input-channel-{ch.lower()}:mode': {'command': 'INTYPE', 'vals': {'VOLTAGE': 0, 'CURRENT': 1}} for ch in ALLOWED_INPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:input-channel-{ch.lower()}:excitation-range': {'command': 'INTYPE', 'vals': {'RANGE_2_MICRO_VOLTS': 1,
 'RANGE_6_POINT_32_MICRO_VOLTS': 2,
 'RANGE_20_MICRO_VOLTS': 3,
 'RANGE_63_POINT_2_MICRO_VOLTS': 4,
 'RANGE_200_MICRO_VOLTS': 5,
 'RANGE_632_MICRO_VOLTS': 6,
 'RANGE_2_MILLI_VOLTS': 7,
 'RANGE_6_POINT_32_MILLI_VOLTS': 8,
 'RANGE_20_MILLI_VOLTS': 9,
 'RANGE_63_POINT_2_MILLI_VOLTS': 10,
 'RANGE_200_MILLI_VOLTS': 11,
 'RANGE_632_MILLI_VOLTS': 12,
 'RANGE_1_PICO_AMP': 1,
 'RANGE_3_POINT_16_PICO_AMPS': 2,
 'RANGE_10_PICO_AMPS': 3,
 'RANGE_31_POINT_6_PICO_AMPS': 4,
 'RANGE_100_PICO_AMPS': 5,
 'RANGE_316_PICO_AMPS': 6,
 'RANGE_1_NANO_AMP': 7,
 'RANGE_3_POINT_16_NANO_AMPS': 8,
 'RANGE_10_NANO_AMPS': 9,
 'RANGE_31_POINT_6_NANO_AMPS': 10,
 'RANGE_100_NANO_AMPS': 11,
 'RANGE_316_NANO_AMPS': 12,
 'RANGE_1_MICRO_AMP': 13,
 'RANGE_3_POINT_16_MICRO_AMPS': 14,
 'RANGE_10_MICRO_AMPS': 15,
 'RANGE_31_POINT_6_MICRO_AMPS': 16,
 'RANGE_100_MICRO_AMPS': 17,
 'RANGE_316_MICRO_AMPS': 18,
 'RANGE_1_MILLI_AMP': 19,
 'RANGE_3_POINT_16_MILLI_AMPS': 20,
 'RANGE_10_MILLI_AMPS': 21,
 'RANGE_31_POINT_6_MILLI_AMPS': 22}} for ch in ALLOWED_INPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:input-channel-{ch.lower()}:auto-range': {'command': 'INTYPE', 'vals': {'OFF': 0, 'CURRENT': 1, 'ROX102B': 2}} for ch in ALLOWED_INPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:input-channel-{ch.lower()}:current-source-shunted': {'command': 'INTYPE', 'vals': {'EXCITATION OFF': False, 'EXCITATION ON': True}} for ch in ALLOWED_INPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:input-channel-{ch.lower()}:units': {'command': 'INTYPE', 'vals': {'KELVIN': 1, 'OHMS': 2}} for ch in ALLOWED_INPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:input-channel-{ch.lower()}:resistance-range': {'command': 'INTYPE', 'vals': {'RANGE_2_MILLI_OHMS': 1,
 'RANGE_6_POINT_32_MILLI_OHMS': 2,
 'RANGE_20_MILLI_OHMS': 3,
 'RANGE_63_POINT_2_MILLI_OHMS': 4,
 'RANGE_200_MILLI_OHMS': 5,
 'RANGE_632_MILLI_OHMS': 6,
 'RANGE_2_OHMS': 7,
 'RANGE_6_POINT_32_OHMS': 8,
 'RANGE_20_OHMS': 9,
 'RANGE_63_POINT_2_OHMS': 10,
 'RANGE_200_OHMS': 11,
 'RANGE_632_OHMS': 12,
 'RANGE_2_KIL_OHMS': 13,
 'RANGE_6_POINT_32_KIL_OHMS': 14,
 'RANGE_20_KIL_OHMS': 15,
 'RANGE_63_POINT_2_KIL_OHMS': 16,
 'RANGE_200_KIL_OHMS': 17,
 'RANGE_632_KIL_OHMS': 18,
 'RANGE_2_MEGA_OHMS': 19,
 'RANGE_6_POINT_32_MEGA_OHMS': 20,
 'RANGE_20_MEGA_OHMS': 21,
 'RANGE_63_POINT_2_MEGA_OHMS': 22}} for ch in ALLOWED_INPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:input-channel-{ch.lower()}:enable': {'command': 'INSET', 'vals': {'ENABLED': True, 'DISABLED': False}} for ch in ALLOWED_INPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:input-channel-{ch.lower()}:dwell-time': {'command': 'INSET', 'vals': [0, 200]} for ch in ALLOWED_INPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:input-channel-{ch.lower()}:pause-time': {'command': 'INSET', 'vals': [3, 200]} for ch in ALLOWED_INPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:input-channel-{ch.lower()}:curve-number': {'command': 'INSET', 'vals': {str(cn): cn for cn in np.arange(1,60)}} for ch in ALLOWED_INPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:input-channel-{ch.lower()}:temperatur-coefficient': {'command': 'INSET', 'vals': {'NEGATIVE': 1, 'POSITIVE': 2}} for ch in ALLOWED_INPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:heater-channel-{ch}:output-mode': {'command': 'OUTMODE', 'vals': {'OFF': 0,
 'MONITOR_OUT': 1,
 'OPEN_LOOP': 2,
 'ZONE': 3,
 'STILL': 4,
 'CLOSED_LOOP': 5,
 'WARMUP': 6}} for ch in ALLOWED_OUTPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:heater-channel-{ch}:input-channel': {'command': 'OUTMODE', 'vals': {'NONE': 0,
 'ONE': 1,
 'TWO': 2,
 'THREE': 3,
 'FOUR': 4,
 'FIVE': 5,
 'SIX': 6,
 'SEVEN': 7,
 'EIGHT': 8,
 'NINE': 9,
 'TEN': 10,
 'ELEVEN': 11,
 'TWELVE': 12,
 'THIRTEEN': 13,
 'FOURTEEN': 14,
 'FIFTEEN': 15,
 'SIXTEEN': 16,
 'CONTROL': 'A'}} for ch in ALLOWED_OUTPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:heater-channel-{ch}:powerup-enable': {'command': 'OUTMODE', 'vals': {'ENABLED': True, 'DISABLED': False}} for ch in ALLOWED_OUTPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:heater-channel-{ch}:reading-filter': {'command': 'OUTMODE', 'vals': {'ENABLED': True, 'DISABLED': False}} for ch in ALLOWED_OUTPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:heater-channel-{ch}:delay': {'command': 'OUTMODE', 'vals': [1, 255]} for ch in ALLOWED_OUTPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:heater-channel-{ch}:polarity': {'command': 'OUTMODE', 'vals': {'UNIPOLAR': 0, 'BIPOLAR': 1}} for ch in ALLOWED_OUTPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:heater-channel-{ch}:setpoint': {'command': 'SETP', 'vals': [0, 4]} for ch in ALLOWED_OUTPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:heater-channel-{ch}:gain': {'command': 'PID', 'vals': [0, 1000]} for ch in ALLOWED_OUTPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:heater-channel-{ch}:integral': {'command': 'PID', 'vals': [0, 10000]} for ch in ALLOWED_OUTPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:heater-channel-{ch}:derivative': {'command': 'PID', 'vals': [0, 2500]} for ch in ALLOWED_OUTPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:heater-channel-{ch}:range': {'command': 'RANGE', 'vals': {'OFF': 0, 'ON': True,
 'RANGE_31_POINT_6_MICRO_AMPS': 1,
 'RANGE_100_MICRO_AMPS': 2,
 'RANGE_316_MICRO_AMPS': 3,
 'RANGE_1_MILLI_AMP': 4,
 'RANGE_3_POINT_16_MILLI_AMPS': 5,
 'RANGE_10_MILLI_AMPS': 6,
 'RANGE_31_POINT_6_MILLI_AMPS': 7,
 'RANGE_100_MILLI_AMPS': 8}} for ch in ALLOWED_OUTPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:curve-{cu}:curve-name': {'command': 'CRVHDR', 'vals': None} for cu in np.arange(21, 60)})
COMMANDS372.update({f'device-settings:ls372:curve-{cu}:serial-number': {'command': 'CRVHDR', 'vals': None} for cu in np.arange(21, 60)})
COMMANDS372.update({f'device-settings:ls372:curve-{cu}:curve-data-format': {'command': 'CRVHDR', 'vals': {'MILLIVOLT_PER_KELVIN': 1,
                                                                                                            'VOLTS_PER_KELVIN': 2,
                                                                                                            'OHMS_PER_KELVIN': 3,
                                                                                                            'LOG_OHMS_PER_KELVIN': 4}} for cu in np.arange(21, 60)})
COMMANDS372.update({f'device-settings:ls372:curve-{cu}:temperature-limit': {'command': 'CRVHDR', 'vals': [0, 400]} for cu in np.arange(21, 60)})
COMMANDS372.update({f'device-settings:ls372:curve-{cu}:coefficient': {'command': 'CRVHDR', 'vals': {'NEGATIVE': 1,
                                                                                                      'POSITIVE': 2}} for cu in np.arange(21, 60)})

TEMPERATURE_KEY = 'status:temps:device-stage:temp'
RESISTANCE_KEY = 'status:temps:device-stage:resistance'
EXCITATION_POWER_KEY = 'status:temps:device-stage:excitation-power'

TS_KEYS = (TEMPERATURE_KEY, RESISTANCE_KEY, EXCITATION_POWER_KEY)

STATUS_KEY = 'status:device:ls372:status'
FIRMWARE_KEY = "status:device:ls372:firmware"
MODEL_KEY = 'status:device:ls372:model'
SN_KEY = 'status:device:ls372:sn'

QUERY_INTERVAL = 1

SETTING_KEYS = tuple(COMMANDS372.keys())

COMMAND_KEYS = [f"command:{k}" for k in SETTING_KEYS]


class LakeShore372Command:
    def __init__(self, schema_key, value=None):
        """
        Initializes a LakeShore336Command. Takes in a redis device-setting:* key and desired value an evaluates it for
        its type, the mapping of the command, and appropriately sets the mapping|range for the command. If the setting
        is not supported, raise a ValueError.
        """

        if schema_key not in COMMANDS372.keys():
            raise ValueError(f'Unknown command: {schema_key}')

        self.range = None
        self.mapping = None
        self.value = value
        self.setting = schema_key

        self.command = COMMANDS372[self.setting]['command']
        setting_vals = COMMANDS372[self.setting]['vals']

        if isinstance(setting_vals, dict):
            self.mapping = setting_vals
        else:
            self.range = setting_vals
        self._vet()

    def _vet(self):
        value = self.value

        if self.mapping is not None:
            if value not in self.mapping:
                raise ValueError(f"Invalid value: {value} Options are: {list(self.mapping.keys())}.")
        elif self.range is not None:
            try:
                self.value = float(value)
            except ValueError:
                raise ValueError(f'Invalid value {value}, must be castable to float.')
            if not self.range[0] <= self.value <= self.range[1]:
                raise ValueError(f'Invalid value {value}, must in {self.range}.')
        else:
            self.value = str(value)

    def __str__(self):
        return f"{self.setting_field}->{self.command_value}"

    @property
    def command_code(self):
        return self.command

    @property
    def setting_field(self):
        return self.setting.split(":")[-1].replace('-', '_')

    @property
    def command_value(self):
        if self.mapping is not None:
            return self.mapping[self.value]
        else:
            return self.value

    @property
    def desired_setting(self):
        return {self.setting_field: self.command_value}

    @property
    def channel(self):
        id_str = self.setting.split(":")[2]
        return id_str[-1] if 'channel' in id_str else None

    @property
    def curve(self):
        id_str = self.setting.split(":")[2]
        return id_str[-1] if 'curve' in id_str else None


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

    def configure_input_sensor(self, channel, command_code, **desired_settings):
        new_settings = self._generate_new_settings(channel=channel, command_code=command_code, **desired_settings)

        if channel == "A":
            new_settings['excitation_range'] = Model372ControlInputCurrentRange(new_settings['excitation_range'])
            new_settings['resistance_range'] = 0
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

        # return settings
        try:
            log.info(f"Configuring input sensor on channel {channel}: {settings}")
            # self.configure_input(input_channel=channel, settings=settings)
        except (SerialException, IOError) as e:
            log.error(f"...failed: {e}")
            raise e

    def modify_channel_settings(self, channel, command_code, **desired_settings):
        new_settings = self._generate_new_settings(channel=channel, command_code=command_code, **desired_settings)

        settings = Model372InputChannelSettings(enable=new_settings['enable'],
                                                dwell_time=new_settings['dwell_time'],
                                                pause_time=new_settings['pause_time'],
                                                curve_number=new_settings['curve_number'],
                                                temperature_coefficient=Model372CurveTemperatureCoefficient(new_settings['temperature_coefficient']))

        # return settings
        try:
            log.info(f"Configuring input channel {channel} parameters: {settings}")
            # self.set_input_channel_parameters(channel, settings)
        except (SerialException, IOError) as e:
            log.error(f"...failed: {e}")
            raise e

    def configure_heater_settings(self, channel, command_code, **desired_settings):

        new_settings = self._generate_new_settings(channel=channel, command_code=command_code, **desired_settings)

        settings = Model372HeaterOutputSettings(output_mode=Model372OutputMode(new_settings['output_mode']),
                                                input_channel=Model372InputChannel(new_settings['input_channel']),
                                                powerup_enable=new_settings['powerup_enable'],
                                                reading_filter=new_settings['reading_filter'],
                                                delay=new_settings['delay'],
                                                polarity=Model372Polarity(new_settings['polarity']))

        # return settings
        try:
            log.info(f"Configuring heater for output channel {channel}: {settings}")
            self.configure_heater(output_channel=channel, settings=settings)
        except (SerialException, IOError) as e:
            log.error(f"...failed: {e}")
            raise e

    def change_temperature_setpoint(self, channel, command_code, setpoint=None):
        current_setpoint = self.query_settings(command_code, channel)
        if current_setpoint != setpoint and setpoint is not None:
            log.info(f"Changing temperature regulation value for output channel {channel} to {setpoint} from "
                     f"{current_setpoint}")
            # return setpoint
            try:
                log.info(f"Changing the setpoint for output channel {channel} to {setpoint}")
                self.set_setpoint_kelvin(output_channel=channel, setpoint=setpoint)
            except (SerialException, IOError) as e:
                log.error(f"...failed: {e}")
                raise e
        else:
            log.info(f"Requested to set temperature setpoint from {current_setpoint} to {setpoint}, no change"
                        f"sent to Lake Shore 372.")

    def modify_pid_settings(self, channel, command_code, **desired_settings):
        new_settings = self._generate_new_settings(channel=channel, command_code=command_code, **desired_settings)

        # return new_settings
        try:
            log.info(f"Configuring PID for output channel {channel}: {new_settings}")
            self.set_heater_pid(channel, gain=new_settings['gain'], integral=new_settings['integral'],
                                derivative=new_settings['ramp_rate'])
        except (SerialException, IOError) as e:
            log.error(f"...failed: {e}")
            raise e

    def modify_heater_output_range(self, channel, command_code, range=None):
        current_range = self.query_settings(command_code, channel)

        if channel == 0:
            if current_range.value == range or range is None:
                log.info(f"Attempting to set the output range for the output heater from {current_range.name} to the "
                         f"same value. No change requested to the instrument.")
            else:
                try:
                    log.info(f"Setting the output range of channel {channel} from {current_range} to {range}")
                    self.set_heater_output_range(channel, Model372SampleHeaterOutputRange(range))
                except (SerialException, IOError) as e:
                    log.error(f"...failed: {e}")
                    raise e
        else:
            # For a channel that is not the sample heater, this value must be a percentage
            if current_range == range or range is None:
                log.info(f"Attempting to set the output range for the output heater from {current_range} to the "
                         f"same value. No change requested to the instrument.")
            else:
                try:
                    log.info(f"Setting the output range of channel {channel} from {current_range} to {range}")
                    self.set_heater_output_range(channel, range)
                except (SerialException, IOError) as e:
                    log.error(f"...failed: {e}")
                    raise e


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

    def callback(temperature, resistance, excitation):
        d = {k: x for k, x in zip((TEMPERATURE_KEY, RESISTANCE_KEY, EXCITATION_POWER_KEY), (temperature, resistance, excitation)) if x}
        redis.store(d, timeseries=True)

    lakeshore.monitor(QUERY_INTERVAL, (lakeshore.temp, lakeshore.sensor_vals, lakeshore.excitation_power), value_callback=callback)

    try:
        while True:
            for key, val in redis.listen(COMMAND_KEYS):
                log.debug(f"heard {key} -> {val}!")
                try:
                    cmd = LakeShore372Command(key.removeprefix('command:'), val)
                except ValueError as e:
                    log.warning(f"Ignoring invalid command ('{key}={val}'): {e}")
                    continue
                try:
                    log.info(f"Processing command '{cmd}'")
                    if cmd.command_code == "INTYPE":
                        lakeshore.configure_input_sensor(channel=cmd.channel, command_code=cmd.command_code, **cmd.desired_setting)
                    elif cmd.command_code == "INSET":
                        lakeshore.modify_channel_settings(channel=cmd.channel, command_code=cmd.command_code, **cmd.desired_setting)
                    elif cmd.command_code == "OUTMODE":
                        lakeshore.configure_heater_settings(channel=cmd.channel, command_code=cmd.command_code, **cmd.desired_setting)
                    elif cmd.command_code == "SETP":
                        lakeshore.change_temperature_setpoint(channel=cmd.channel, command_code=cmd.command_code, setpoint=cmd.command_value)
                    elif cmd.command_code == "PID":
                        lakeshore.modify_pid_settings(channel=cmd.channel, command_code=cmd.command_code, **cmd.desired_setting)
                    elif cmd.command_code == "RANGE":
                        lakeshore.modify_heater_output_range(channel=cmd.channel, command_code=cmd.command_code, range=cmd.command_value)
                    elif cmd.command_code == "CRVHDR":
                        lakeshore.modify_curve_header(curve_num=cmd.curve, command_code=cmd.command_code, **cmd.desired_setting)
                    else:
                        log.info(f"Command code '{cmd.command_code}' not recognized! No change will be made")
                        pass
                    redis.store({cmd.setting: cmd.value})
                    redis.store({STATUS_KEY: "OK"})
                except IOError as e:
                    redis.store({STATUS_KEY: f"Error {e}"})
                    log.error(f"Comm error: {e}")
    except RedisError as e:
        log.critical(f"Redis server error! {e}")
        sys.exit(1)

    print('go')
