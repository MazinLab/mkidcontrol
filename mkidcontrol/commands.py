"""
Author: Noah Swimmer, 10 May 2022
"""

import numpy as np

class SimCommand:
    def __init__(self, schema_key, value=None):
        """
        Initializes a SimCommand. Takes in a redis device-setting:* key and desired value an evaluates it for its type,
        the mapping of the command, and appropriately sets the mapping|range for the command. If the setting is not
        supported, raise a ValueError.

        If no value is specified it will create the command as a query
        """
        if schema_key not in COMMAND_DICT.keys():
            raise ValueError(f'Unknown command: {schema_key}')

        self.range = None
        self.mapping = None
        self.value = value
        self.setting = schema_key

        self.command = COMMAND_DICT[self.setting]['command']
        setting_vals = COMMAND_DICT[self.setting]['vals']

        if isinstance(setting_vals, dict):
            self.mapping = setting_vals
        else:
            self.range = setting_vals

        self._vet()

    def _vet(self):
        """Verifies value agaisnt papping or range and handles necessary casting"""
        if self.value is None:
            return True

        value = self.value
        if self.mapping is not None:
            if value not in self.mapping:
                raise ValueError(f'Invalid value {value}. Options are: {list(self.mapping.keys())}.')
        else:
            try:
                self.value = float(value)
            except ValueError:
                raise ValueError(f'Invalid value {value}, must be castable to float.')
            if not self.range[0] <= self.value <= self.range[1]:
                raise ValueError(f'Invalid value {value}, must in {self.range}.')

    def __str__(self):
        return f"{self.setting}->{self.value}"

    @property
    def is_query(self):
        return self.value is None

    @property
    def ls_string(self):
        """
        Returns the command string for the SIM.
        """
        if self.is_query:
            return self.ls_query_string

        v = self.mapping[self.value] if self.range is None else self.value
        return f"{self.command} {v}"

    @property
    def ls_query_string(self):
        """ Returns the corresponding command string to query for the setting"""
        return f"{self.command}?"


class LakeShoreCommand:
    def __init__(self, schema_key, value=None, limit_vals:dict=None):
        """
        Initializes a LakeShore336Command. Takes in a redis device-setting:* key and desired value an evaluates it for
        its type, the mapping of the command, and appropriately sets the mapping|range for the command. If the setting
        is not supported, raise a ValueError.
        """

        if schema_key not in COMMAND_DICT.keys():
            raise ValueError(f'Unknown command: {schema_key}')

        if schema_key[-5:] == 'limit' and not limit_vals:
            raise ValueError(f"Cannot handle command for {schema_key} without the existing limit values")

        self.range = None
        self.str_value = None
        self.mapping = None
        self.value = value
        self.setting = schema_key
        self.limit_values = limit_vals

        self.command = COMMAND_DICT[self.setting]['command']
        setting_vals = COMMAND_DICT[self.setting]['vals']

        if isinstance(setting_vals, dict):
            self.mapping = setting_vals
        elif isinstance(setting_vals, str):
            self.str_value = setting_vals
        else:
            self.range = setting_vals
        self._vet()

    def _vet(self):
        """Verifies value agaisnt papping or range and handles necessary casting"""
        if self.value is None:
            return True

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
        elif self.str_value is not None:
            self.value = str(value)[:15]

    def __str__(self):
        return f"{self.setting_field}->{self.command_value}"

    ### Properties below are used with LakeShore 336 and 372, which have different command handling syntax than the 625
    ### due to LakeShore providing robust wrappers for the former 2, but not the latter
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
    ### End LS 336 and 372 properties

    ### Properties below are used with LakeShore625
    @property
    def is_query(self):
        return self.value is None

    def _parse_limit_values(self):
        field_to_change = self.setting.split(":")[-1][:-6]
        if field_to_change in ('current', 'voltage', 'rate'):
            self.limit_values[field_to_change] = self.value
        else:
            raise ValueError(f"Unknown limit field: {field_to_change}")

        return f"{self.limit_values['current']}, {self.limit_values['voltage']}, {self.limit_values['rate']}"

    @property
    def ls_string(self):
        """
        Returns the command string for the SIM.
        """
        if self.is_query:
            return self.ls_query_string

        if self.command == "LIMIT":
            v = self._parse_limit_values()
        else:
            v = self.mapping[self.value] if self.range is None else self.value
        return f"{self.command} {v}"

    @property
    def ls_query_string(self):
        """ Returns the corresponding command string to query for the setting"""
        if self.command[-1] == ",":
            return f"{self.command[:-3]}?"
        else:
            return f"{self.command}?"


def load_tvals(curve):
    if curve == 1:
        file = '/home/kids/mkidcontrol/docs/hardware_reference_documentation/thermometry/RX-102A/RX-102A_Mean_Curve.tbl'
        # import pkg_resources as pkg
        # file = pkg.resource_filename('hardware.thermometry.RX-102A', 'RX-102A_Mean_Curve.tbl')
    else:
        return 0

    try:
        curve_data = np.loadtxt(file)
        temp_data = curve_data[:, 0]
    except OSError:
        log.error(f"Could not find curve data file.")
        raise ValueError(f"{file} couldn't be loaded.")
    except IndexError:
        raise ValueError(f"{file} couldn't be loaded.")

    return {str(i): i for i in temp_data}


# ---- Lake Shore 336 Commands ----
ENABLED_336_CHANNELS = ('C', 'D')
ALLOWED_336_CHANNELS = ("A", "B", "C", "D")

from lakeshore import Model336InputSensorType, Model336InputSensorUnits, \
    Model336DiodeRange, Model336RTDRange, Model336ThermocoupleRange, Model336CurveFormat, \
    Model336CurveTemperatureCoefficients

LS336_INPUT_SENSOR_TYPES = {key: val.value for key, val in zip(['Disabled', 'Diode', 'Platinum RTD', 'NTC RTD', 'Thermocouple', 'Capacitance'], Model336InputSensorType)}
LS336_INPUT_SENSOR_UNITS = {key: val.value for key, val in zip(['Kelvin', 'Celsius', 'Sensor'], Model336InputSensorUnits)}
LS336_DIODE_RANGE = {key: val.value for key, val in zip(["2.5 V", "10 V"], Model336DiodeRange)}
LS336_RTD_RANGE = {key: val.value for key, val in zip([f"{res} \u03A9" for res in [10, 30, 100, 300, 1e3, 3e3, 10e3, 30e3, 100e3]], Model336RTDRange)}
LS336_THERMOCOUPLE_RANGE = {key: val.value for key, val in zip(['50 mV'], Model336ThermocoupleRange)}
LS336_AUTORANGE_VALUES = {'False': False, 'True': True}
LS336_COMPENSATION_VALUES = {'False': False, 'True': True}
LS336_CURVE_DATA_FORMAT = {key: val.value for key, val in zip(['mV/K', 'V/K', '\u03A9/K', 'log(\u03A9)/K'], Model336CurveFormat)}
LS336_CURVE_COEFFICIENTS = {key: val.value for key, val in zip(['Negative', 'Positive'], Model336CurveTemperatureCoefficients)}

LS336_INPUT_SENSOR_RANGE = {}
LS336_INPUT_SENSOR_RANGE.update(LS336_DIODE_RANGE)
LS336_INPUT_SENSOR_RANGE.update(LS336_RTD_RANGE)
LS336_INPUT_SENSOR_RANGE.update(LS336_THERMOCOUPLE_RANGE)


class LS336InputSensor:
    def __init__(self, channel, redis):
        values = redis.read(redis.redis_keys(f'device-settings*ls336:input-channel-{channel.lower()}*'))
        self.channel = channel.upper()

        self.name = values[f'device-settings:ls336:input-channel-{channel.lower()}:name']
        self.sensor_type = values[f'device-settings:ls336:input-channel-{channel.lower()}:sensor-type']
        self.input_range = values[f'device-settings:ls336:input-channel-{channel.lower()}:input-range']
        self.autorange_enabled = values[f'device-settings:ls336:input-channel-{channel.lower()}:autorange-enable']
        self.compensation = values[f'device-settings:ls336:input-channel-{channel.lower()}:compensation']
        self.curve = values[f'device-settings:ls336:input-channel-{channel.lower()}:curve']
        self.units = values[f'device-settings:ls336:input-channel-{channel.lower()}:units']


COMMANDS336 = {}
COMMANDS336.update({f'device-settings:ls336:input-channel-{ch.lower()}:name': {'command': f'INNAME {ch.upper()}', 'vals': ''} for ch in ALLOWED_336_CHANNELS})
COMMANDS336.update({f'device-settings:ls336:input-channel-{ch.lower()}:sensor-type': {'command': 'INTYPE', 'vals': LS336_INPUT_SENSOR_TYPES} for ch in ALLOWED_336_CHANNELS})
COMMANDS336.update({f'device-settings:ls336:input-channel-{ch.lower()}:autorange-enable': {'command': 'INTYPE', 'vals': LS336_AUTORANGE_VALUES} for ch in ALLOWED_336_CHANNELS})
COMMANDS336.update({f'device-settings:ls336:input-channel-{ch.lower()}:compensation': {'command': 'INTYPE', 'vals': LS336_COMPENSATION_VALUES} for ch in ALLOWED_336_CHANNELS})
COMMANDS336.update({f'device-settings:ls336:input-channel-{ch.lower()}:units': {'command': 'INTYPE', 'vals': LS336_INPUT_SENSOR_UNITS} for ch in ALLOWED_336_CHANNELS})
COMMANDS336.update({f'device-settings:ls336:input-channel-{ch.lower()}:input-range': {'command': 'INTYPE', 'vals': LS336_INPUT_SENSOR_RANGE} for ch in ALLOWED_336_CHANNELS})
COMMANDS336.update({f'device-settings:ls336:input-channel-{ch.lower()}:curve': {'command': 'INCRV', 'vals': {str(cn): cn for cn in np.arange(1, 60)}} for ch in ALLOWED_336_CHANNELS})
COMMANDS336.update({f'device-settings:ls336:curve-{cu}:curve-name': {'command': 'CRVHDR', 'vals': None} for cu in np.arange(21, 60)})
COMMANDS336.update({f'device-settings:ls336:curve-{cu}:serial-number': {'command': 'CRVHDR', 'vals': None} for cu in np.arange(21, 60)})
COMMANDS336.update({f'device-settings:ls336:curve-{cu}:curve-data-format': {'command': 'CRVHDR', 'vals': LS336_CURVE_DATA_FORMAT} for cu in np.arange(21, 60)})
COMMANDS336.update({f'device-settings:ls336:curve-{cu}:temperature-limit': {'command': 'CRVHDR', 'vals': [0, 400]} for cu in np.arange(1, 60)})
COMMANDS336.update({f'device-settings:ls336:curve-{cu}:coefficient': {'command': 'CRVHDR', 'vals': LS336_CURVE_COEFFICIENTS} for cu in np.arange(21, 60)})


# ---- Lake Shore 372 Commands ----
ENABLED_372_INPUT_CHANNELS = ("A", "1")
ALLOWED_372_INPUT_CHANNELS = ("A", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12", "13", "14", "15", "16")
ENABLED_372_OUTPUT_CHANNELS = (0, )
ALLOWED_372_OUTPUT_CHANNELS = (0, 1, 2)

from lakeshore import Model372SensorExcitationMode, Model372MeasurementInputCurrentRange, \
    Model372MeasurementInputVoltageRange, Model372CurveFormat, Model372CurveTemperatureCoefficient, \
    Model372AutoRangeMode, Model372InputSensorUnits, Model372MeasurementInputResistance, Model372OutputMode, \
    Model372InputChannel, Model372ControlInputCurrentRange, Model372Polarity, Model372SampleHeaterOutputRange

LS372_SENSOR_MODE = {key: val.value for key, val in zip(['Voltage', 'Current'], Model372SensorExcitationMode)}
LS372_AUTORANGE_VALUES = {key: val.value for key, val in zip(['Off', 'Current', 'ROX102B'], Model372AutoRangeMode)}
LS372_MEASUREMENT_INPUT_VOLTAGE_RANGE = {key: val.value for key, val in zip(['2 \u00B5V', '6.32 \u00B5V', '20 \u00B5V', '63.2 \u00B5V', '200 \u00B5V', '632 \u00B5V',
                                                                 '2 mV', '6.32 mV', '20 mV', '63.2 mV', '200 mV', '632 mV'], Model372MeasurementInputVoltageRange)}
LS372_MEASUREMENT_INPUT_CURRENT_RANGE = {key: val.value for key, val in zip(['1 pA', '3.16 pA', '10 pA', '31.6 pA', '100 pA', '316 pA',
                                                                 '1 nA', '3.16 nA', '10 nA', '31.6 nA', '100 nA', '316 nA',
                                                                 '1 \u00B5A', '3.16 \u00B5A', '10 \u00B5A', '31.6 \u00B5A', '100 \u00B5A', '316 \u00B5A',
                                                                 '1 mA', '3.16 mA', '10 mA', '31.6 mA'], Model372MeasurementInputCurrentRange)}
LS372_CONTROL_INPUT_CURRENT_RANGE = {key: val.value for key, val in zip(['316 pA', '1 nA', '3.16 nA', '10 nA', '31.6 nA', '100 nA'], Model372ControlInputCurrentRange)}
LS372_CURRENT_SOURCE_SHUNTED_VALUES = {"False": False, "True": True}
LS372_INPUT_SENSOR_UNITS = {key: val.value for key, val in zip(['Kelvin', 'Ohms'], Model372InputSensorUnits)}
LS372_INPUT_FILTER_STATES = {'Off': False, 'On': True}
LS372_RESISTANCE_RANGE = {key: val.value for key, val in zip([f"{res}\u03A9" for res in ['2 m', '6.32 m', '20 m', '63.2 m', '200 m', '632 m',
                                                                                         '2 ', '6.32 ', '20 ', '63.2 ', '200 ', '632 ',
                                                                                         '2 k', '6.32 k', '20 k', '63.2 k', '200 k', '632 k',
                                                                                         '2 M', '6.32 M', '20 M', '63.2 M']], Model372MeasurementInputResistance)}
LS372_ENABLED_VALUES = {'False': False, 'True': True}
LS372_HEATER_OUTPUT_MODE = {key: val.value for key, val in zip(['Off', 'Monitor Out', 'Open Loop', 'Zone', 'Still', 'Closed Loop', 'Warmup'], Model372OutputMode)}
LS372_HEATER_INPUT_CHANNEL = {key: val.value for key, val in zip(['None', 'One', 'Two', 'Three', 'Four', 'Five', 'Six',
                                                                  'Seven', 'Eight', 'Nine', 'Ten', 'Eleven', 'Twelve',
                                                                  'Thirteen', 'Fourteen', 'Fifteen', 'Sixteen', 'Control'], Model372InputChannel)}
LS372_HEATER_POWERUP_ENABLE = {'False': False, 'True': True}
LS372_HEATER_READING_FILTER = {'False': False, 'True': True}
LS372_OUTPUT_POLARITY = {key: val.value for key, val in zip(['Unipolar', 'Bipolar'], Model372Polarity)}
LS372_HEATER_CURRENT_RANGE = {key: val.value for key, val in zip(['Off', '31.6 \u00B5A', '100 \u00B5A', '316 \u00B5A', '1 mA',
                                                             '3.16 mA', '10 mA', '31.6 mA', '100 mA'], Model372SampleHeaterOutputRange)}
LS372_CURVE_DATA_FORMAT = {key: val.value for key, val in zip(['\u03A9/K', 'log(\u03A9)/K', '\u03A9/K Cubic Spine'], Model372CurveFormat)}
LS372_CURVE_COEFFICIENTS = {key: val.value for key, val in zip(['Negative', 'Positive'], Model372CurveTemperatureCoefficient)}

LS372_INPUT_SENSOR_RANGE = {}
LS372_INPUT_SENSOR_RANGE.update(LS372_MEASUREMENT_INPUT_VOLTAGE_RANGE)
LS372_INPUT_SENSOR_RANGE.update(LS372_MEASUREMENT_INPUT_CURRENT_RANGE)


class LS372InputSensor:
    def __init__(self, channel, redis):
        values = redis.read(redis.redis_keys(f'device-settings*ls372:input-channel-{channel.lower()}*'))
        self.channel = channel.upper()

        self.name = values[f'device-settings:ls372:input-channel-{channel.lower()}:name']
        self.mode = values[f'device-settings:ls372:input-channel-{channel.lower()}:mode']
        self.excitation_range = values[f'device-settings:ls372:input-channel-{channel.lower()}:excitation-range']
        self.auto_range = values[f'device-settings:ls372:input-channel-{channel.lower()}:auto-range']
        self.current_source_shunted = values[f'device-settings:ls372:input-channel-{channel.lower()}:current-source-shunted']
        self.units = values[f'device-settings:ls372:input-channel-{channel.lower()}:units']
        self.resistance_range = values[f'device-settings:ls372:input-channel-{channel.lower()}:resistance-range']
        self.enable = values[f'device-settings:ls372:input-channel-{channel.lower()}:enable']
        self.dwell_time = float(values[f'device-settings:ls372:input-channel-{channel.lower()}:dwell-time'])
        self.pause_time = float(values[f'device-settings:ls372:input-channel-{channel.lower()}:pause-time'])
        self.curve_number = values[f'device-settings:ls372:input-channel-{channel.lower()}:curve-number']
        self.temperature_coefficient = values[f'device-settings:ls372:input-channel-{channel.lower()}:temperature-coefficient']

        # Filter values
        self.state = values[f'device-settings:ls372:input-channel-{channel.lower()}:filter:state']
        self.settle_time = values[f'device-settings:ls372:input-channel-{channel.lower()}:filter:settle-time']
        self.window = values[f'device-settings:ls372:input-channel-{channel.lower()}:filter:window']

class LS372HeaterOutput:
    def __init__(self, channel, redis):
        # N.B. Heater channels are numbers, no need to match lower/upper-case
        values = redis.read(redis.redis_keys(f'device-settings:ls372:heater-channel-{channel}:*'))
        self.channel = channel
        if int(channel) == 0:
            self.name = "Device (Sample Heater)"
        elif int(channel) == 1:
            self.name = "Warm-up Heater"
        elif int(channel) == 2:
            self.name = "Analog/Still Heater"

        self.output_mode = values[f'device-settings:ls372:heater-channel-{channel}:output-mode']
        self.input_channel = values[f'device-settings:ls372:heater-channel-{channel}:input-channel']
        self.powerup_enable = values[f'device-settings:ls372:heater-channel-{channel}:powerup-enable']
        self.reading_filter = values[f'device-settings:ls372:heater-channel-{channel}:reading-filter']
        self.delay = float(values[f'device-settings:ls372:heater-channel-{channel}:delay'])
        self.polarity = values[f'device-settings:ls372:heater-channel-{channel}:polarity']
        self.setpoint = values[f'device-settings:ls372:heater-channel-{channel}:setpoint']
        self.gain = values[f'device-settings:ls372:heater-channel-{channel}:gain']
        self.integral = values[f'device-settings:ls372:heater-channel-{channel}:integral']
        self.ramp_rate = values[f'device-settings:ls372:heater-channel-{channel}:ramp_rate']
        self.range = values[f'device-settings:ls372:heater-channel-{channel}:range']


COMMANDS372 = {}
COMMANDS372.update({f'device-settings:ls372:input-channel-{ch.lower()}:name': {'command': 'INNAME', 'vals': ''} for ch in ALLOWED_372_INPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:input-channel-{ch.lower()}:mode': {'command': 'INTYPE', 'vals': LS372_SENSOR_MODE} for ch in ALLOWED_372_INPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:input-channel-{ch.lower()}:excitation-range': {'command': 'INTYPE', 'vals': LS372_INPUT_SENSOR_RANGE} for ch in ALLOWED_372_INPUT_CHANNELS[1:]})
COMMANDS372.update({f'device-settings:ls372:input-channel-{ch.lower()}:excitation-range': {'command': 'INTYPE', 'vals': LS372_CONTROL_INPUT_CURRENT_RANGE} for ch in ALLOWED_372_INPUT_CHANNELS[0]})
COMMANDS372.update({f'device-settings:ls372:input-channel-{ch.lower()}:auto-range': {'command': 'INTYPE', 'vals': LS372_AUTORANGE_VALUES} for ch in ALLOWED_372_INPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:input-channel-{ch.lower()}:current-source-shunted': {'command': 'INTYPE', 'vals': LS372_CURRENT_SOURCE_SHUNTED_VALUES} for ch in ALLOWED_372_INPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:input-channel-{ch.lower()}:units': {'command': 'INTYPE', 'vals': LS372_INPUT_SENSOR_UNITS} for ch in ALLOWED_372_INPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:input-channel-{ch.lower()}:resistance-range': {'command': 'INTYPE', 'vals': LS372_RESISTANCE_RANGE} for ch in ALLOWED_372_INPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:input-channel-{ch.lower()}:enable': {'command': 'INSET', 'vals': LS372_ENABLED_VALUES} for ch in ALLOWED_372_INPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:input-channel-{ch.lower()}:dwell-time': {'command': 'INSET', 'vals': [0, 200]} for ch in ALLOWED_372_INPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:input-channel-{ch.lower()}:pause-time': {'command': 'INSET', 'vals': [3, 200]} for ch in ALLOWED_372_INPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:input-channel-{ch.lower()}:curve-number': {'command': 'INSET', 'vals': {str(cn): cn for cn in np.arange(1,60)}} for ch in ALLOWED_372_INPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:input-channel-{ch.lower()}:temperature-coefficient': {'command': 'INSET', 'vals': LS372_CURVE_COEFFICIENTS} for ch in ALLOWED_372_INPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:input-channel-{ch.lower()}:filter:state': {'command': 'FILTER', 'vals': LS372_INPUT_FILTER_STATES} for ch in ALLOWED_372_INPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:input-channel-{ch.lower()}:filter:settle-time': {'command': 'FILTER', 'vals': [1, 200]} for ch in ALLOWED_372_INPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:input-channel-{ch.lower()}:filter:window': {'command': 'FILTER', 'vals': [1,80]} for ch in ALLOWED_372_INPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:heater-channel-{ch}:output-mode': {'command': 'OUTMODE', 'vals': LS372_HEATER_OUTPUT_MODE} for ch in ALLOWED_372_OUTPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:heater-channel-{ch}:input-channel': {'command': 'OUTMODE', 'vals': LS372_HEATER_INPUT_CHANNEL} for ch in ALLOWED_372_OUTPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:heater-channel-{ch}:powerup-enable': {'command': 'OUTMODE', 'vals': LS372_HEATER_POWERUP_ENABLE} for ch in ALLOWED_372_OUTPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:heater-channel-{ch}:reading-filter': {'command': 'OUTMODE', 'vals': LS372_HEATER_READING_FILTER} for ch in ALLOWED_372_OUTPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:heater-channel-{ch}:delay': {'command': 'OUTMODE', 'vals': [1, 255]} for ch in ALLOWED_372_OUTPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:heater-channel-{ch}:polarity': {'command': 'OUTMODE', 'vals': LS372_OUTPUT_POLARITY} for ch in ALLOWED_372_OUTPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:heater-channel-{ch}:setpoint': {'command': 'SETP', 'vals': [0, 4]} for ch in ALLOWED_372_OUTPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:heater-channel-{ch}:gain': {'command': 'PID', 'vals': [0, 1000]} for ch in ALLOWED_372_OUTPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:heater-channel-{ch}:integral': {'command': 'PID', 'vals': [0, 10000]} for ch in ALLOWED_372_OUTPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:heater-channel-{ch}:ramp_rate': {'command': 'PID', 'vals': [0, 2500]} for ch in ALLOWED_372_OUTPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:heater-channel-{ch}:range': {'command': 'RANGE', 'vals': LS372_HEATER_CURRENT_RANGE} for ch in ALLOWED_372_OUTPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:curve-{cu}:curve-name': {'command': 'CRVHDR', 'vals': None} for cu in np.arange(21, 60)})
COMMANDS372.update({f'device-settings:ls372:curve-{cu}:serial-number': {'command': 'CRVHDR', 'vals': None} for cu in np.arange(21, 60)})
COMMANDS372.update({f'device-settings:ls372:curve-{cu}:curve-data-format': {'command': 'CRVHDR', 'vals': LS372_CURVE_DATA_FORMAT} for cu in np.arange(21, 60)})
COMMANDS372.update({f'device-settings:ls372:curve-{cu}:temperature-limit': {'command': 'CRVHDR', 'vals': [0, 400]} for cu in np.arange(21, 60)})
COMMANDS372.update({f'device-settings:ls372:curve-{cu}:coefficient': {'command': 'CRVHDR', 'vals': LS372_CURVE_COEFFICIENTS} for cu in np.arange(21, 60)})


class LS625MagnetSettings:
    def __init__(self, redis):
        values = redis.read(redis.redis_keys('device-settings:ls625:*'))

        self.baud_rate = values['device-settings:ls625:baud-rate']
        self.current_limit = values['device-settings:ls625:current-limit']
        self.compliance_voltage_limit = values['device-settings:ls625:compliance-voltage-limit']
        self.rate_limit = values['device-settings:ls625:rate-limit']
        self.magnetic_field_parameter = values['device-settings:ls625:magnetic-field-parameter']
        self.quench_ramp_rate = values['device-settings:ls625:quench-ramp-rate']
        self.ramp_rate = values['device-settings:ls625:ramp-rate']
        self.desired_current = values['device-settings:ls625:desired-current']
        self.compliance_voltage = values['device-settings:ls625:compliance-voltage']
        self.control_mode = values['device-settings:ls625:control-mode']

        self.limits = {'current':  self.current_limit, 'voltage':  self.compliance_voltage_limit, 'rate':  self.rate_limit}


# ---- Lake Shore 625 Commands ----
COMMANDS625 = {'device-settings:ls625:baud-rate': {'command': 'BAUD', 'vals': {'9600': '0', '19200': '1',
                                                                               '38400': '2', '57600': '3'}},
               'device-settings:ls625:current-limit': {'command': 'LIMIT', 'vals': [0.0, 60.1000]},
               'device-settings:ls625:compliance-voltage-limit': {'command': 'LIMIT', 'vals': [0.1000, 5.0000]},
               'device-settings:ls625:rate-limit': {'command': 'LIMIT', 'vals': [0.0001, 99.999]},
               'device-settings:ls625:magnetic-field-parameter': {'command': 'FLDS 1,', 'vals': [0.0100, 10.000]},  # Note: For ARCONS = 4.0609 kG/A
               'device-settings:ls625:quench-ramp-rate': {'command': 'QNCH 1,', 'vals': [0.0100, 10.000]},
               'device-settings:ls625:ramp-rate': {'command': 'RATE', 'vals': [0.0001, 99.999]},
               'device-settings:ls625:desired-current': {'command': 'SETI', 'vals': [0.0000, 60.1000]},
               'device-settings:ls625:compliance-voltage': {'command': 'SETV', 'vals': [0.1000, 5.0000]},
               'device-settings:ls625:control-mode': {'command': 'XPGM', 'vals': {'Internal': '0', 'External': '1', 'Sum': '2'}}
               }


# ---- SRS SIM 921 Commands ----
COMMANDS921 = {'device-settings:sim921:resistance-range': {'command': 'RANG', 'vals': {'20e-3': '0', '200e-3': '1', '2': '2', '20': '3', '200': '4', '2e3': '5', '20e3': '6', '200e3': '7', '2e6': '8', '20e6': '9'}},
               'device-settings:sim921:excitation-value': {'command': 'EXCI', 'vals': {'0': '-1', '3e-6': '0', '10e-6': '1', '30e-6': '2', '100e-6': '3', '300e-6': '4', '1e-3': '5', '3e-3': '6', '10e-3': '7', '30e-3': '8'}},
               'device-settings:sim921:excitation-mode': {'command': 'MODE', 'vals': {'passive': '0', 'current': '1', 'voltage': '2', 'power': '3'}},
               'device-settings:sim921:time-constant': {'command': 'TCON', 'vals': {'0.3': '0', '1': '1', '3': '2', '10': '3', '30': '4', '100': '5', '300': '6'}},
               'device-settings:sim921:temp-offset': {'command': 'TSET', 'vals': [0, 40]},
               'device-settings:sim921:resistance-offset': {'command': 'RSET', 'vals': [0, 63765.1]},
               'device-settings:sim921:temp-slope': {'command': 'VKEL', 'vals': [0, 1e-2]},
               'device-settings:sim921:resistance-slope': {'command': 'VOHM', 'vals': [0, 1e-3]},
               'device-settings:sim921:output-mode': {'command': 'AMAN', 'vals': {'scaled': '0', 'manual': '1'}},
               'device-settings:sim921:manual-vout': {'command': 'AOUT', 'vals': [-10, 10]},
               'device-settings:sim921:curve-number': {'command': 'CURV', 'vals': {'1': '1', '2': '2', '3': '3'}},
               }

# ---- SRS SIM 960 Commands ----
COMMANDS960 = {'device-settings:sim960:vout-min-limit': {'command': 'LLIM', 'vals': [-10, 10]},
               'device-settings:sim960:vout-max-limit': {'command': 'ULIM', 'vals': [-10, 10]},
               'device-settings:sim960:vin-setpoint-mode': {'command': 'INPT', 'vals': {'internal': '0', 'external': '1'}},
               'device-settings:sim960:vin-setpoint': {'command': 'SETP', 'vals': [-10, 10]},
               'device-settings:sim960:pid-p:value': {'command': 'GAIN', 'vals': [-1e3, 0]},
               'device-settings:sim960:pid-i:value': {'command': 'INTG', 'vals': [0, 5e5]},
               'device-settings:sim960:pid-d:value': {'command': 'DERV', 'vals': [0, 1e1]},
               'device-settings:sim960:pid-offset:value': {'command': 'OFST', 'vals': [-10,10]},
               'device-settings:sim960:vin-setpoint-slew-enable': {'command': 'RAMP', 'vals': {'off': '0', 'on': '1'}},  # Note: Internal setpoint ramp, NOT magnet ramp
               'device-settings:sim960:vin-setpoint-slew-rate': {'command': 'RATE', 'vals': [1e-3, 1e4]},  # Note: Internal setpoint ramp rate, NOT magnet ramp
               'device-settings:sim960:pid-p:enabled': {'command': 'PCTL', 'vals': {'off': '0', 'on': '1'}},
               'device-settings:sim960:pid-i:enabled': {'command': 'ICTL', 'vals': {'off': '0', 'on': '1'}},
               'device-settings:sim960:pid-d:enabled': {'command': 'DCTL', 'vals': {'off': '0', 'on': '1'}},
               'device-settings:sim960:pid-offset:enabled': {'command': 'OCTL', 'vals': {'off': '0', 'on': '1'}},
               }

# ---- HPD Heat Switch Commands ----
# COMMANDS HS (Heatswitch) are only included so that we can use the SimCommand class to check the legality of a command.
# COMMANDSHS = {'device-settings:currentduino:heatswitch': {'command': '', 'vals': {'open': 'open', 'close': 'close'}}}


class Heatswitch:
    def __init__(self, redis):
        values = redis.read(redis.redis_keys("device-settings:heatswitch:*"))
        self.max_velocity = values['device-settings:heatswitch:max-velocity']
        self.running_current = values['device-settings:heatswitch:running_current']
        self.acceleration = values['device-settings:heatswitch:acceleration']


# ---- Zaber Motor Heat Switch Commands ----
COMMANDSHS = {'device-settings:heatswitch:position': {'command': '', 'vals': {"Open": "Open", "Close": "Close"}},
              'device-settings:heatswitch:max-velocity': {'command': '', 'vals': [0, 1e4]},
              'device-settings:heatswitch:running-current': {'command': '', 'vals': [10, 127]},
              'device-settings:heatswitch:acceleration': {'command': '', 'vals': [0, 100]},
              }


class Laserbox:
    def __init__(self, redis):
        values = redis.read(redis.redis_keys("device-settings:laserflipperduino:*"))
        self.power808 = int(float(values['device-settings:laserflipperduino:laserbox:808:power']))
        self.power904 = int(float(values['device-settings:laserflipperduino:laserbox:904:power']))
        self.power980 = int(float(values['device-settings:laserflipperduino:laserbox:980:power']))
        self.power1120 = int(float(values['device-settings:laserflipperduino:laserbox:1120:power']))
        self.power1310 = int(float(values['device-settings:laserflipperduino:laserbox:1310:power']))
        self.flipperposition = values['device-settings:laserflipperduino:flipper:position']

# ---- Laserflipper Arduino Commands ----
COMMANDSLASERFLIPPER = {'device-settings:laserflipperduino:laserbox:808:power': {'command': '0', 'vals': [0, 100]},
                        'device-settings:laserflipperduino:laserbox:904:power': {'command': '1', 'vals': [0, 100]},
                        'device-settings:laserflipperduino:laserbox:980:power': {'command': '2', 'vals': [0, 100]},
                        'device-settings:laserflipperduino:laserbox:1120:power': {'command': '3', 'vals': [0, 100]},
                        'device-settings:laserflipperduino:laserbox:1310:power': {'command': '4', 'vals': [0, 100]},
                        'device-settings:laserflipperduino:flipper:position': {'command': '', 'vals': {"Up": "Up", "Down": "Down"}}
                        }

FILTERS = {0: 'Closed',
           1: 'Y',
           2: 'Zs',
           3: 'J',
           4: '220+125',
           5: '125',
           6: 'Open'}


class Filterwheel:
    def __init__(self, redis):
        self.filterposition = int(redis.read(redis.redis_keys("device-settings:filterwheel:position")))
        self.filter = f"{self.filterposition}:{FILTERS[self.filterposition]}"


# ---- Fitler Wheel Commands ----
COMMANDSFILTERWHEEL = {'device-settings:filterwheel:position': {'command': '', 'vals': {'0': 0, '1': 1, '2': 2,
                                                                                        '3': 3, '4': 4, '5': 5,
                                                                                        '6': 6}}}


class Focus:
    def __init__(self, redis):
        self.position_mm = redis.read('status:device:focus:position:mm')[1]
        self.position_encoder = redis.read('status:device:focus:position:encoder')[1]


# ---- Focus Slider Commands ----
COMMANDSFOCUS = {'device-settings:focus:home-params:velocity': {'command': '', 'vals': [0, 164931]},
                 'device-settings:focus:home-params:offset-distance': {'command': '', 'vals': [0, 1727750]},
                 'device-settings:focus:home-params:direction': {'command': '', 'vals': {'Reverse': 'reverse', 'Forward': 'forward'}},
                 'device-settings:focus:jog-params:size': {'command': '', 'vals': [0, 100000]},
                 'device-settings:focus:jog-params:acceleration': {'command': '', 'vals': [0, 1000]},
                 'device-settings:focus:jog-params:max-velocity': {'command': '', 'vals': [0, 164931]},
                 'device-settings:focus:jog-params:continuous': {'command': '', 'vals': {'False': False, 'True': True}},
                 'device-settings:focus:move-params:backlash-distance': {'command': '', 'vals': [0, 1000]},
                 'device-settings:focus:velocity-params:acceleration': {'command': '', 'vals': [0, 1000]},
                 'device-settings:focus:velocity-params:max-velocity': {'command': '', 'vals': [0, 164931]},
                 'device-settings:focus:desired-position:encoder': {'command': '', 'vals': [0, 1727750]}}

# ---- PICTURE-C Magnet Commands ----
CALIBRATION_CURVE = 1

# COMMANDS MAGNET are only included so that we can use the SimCommand class to check the legality of a magnet command.
# COMMANDSMAGNET = {'device-settings:sim960:ramp-rate': {'command': '', 'vals': [0, 0.015]},
#                   'device-settings:sim960:deramp-rate': {'command': '', 'vals': [-0.015, 0]},
#                   'device-settings:sim960:soak-time': {'command': '', 'vals': [0, np.inf]},
#                   'device-settings:sim960:soak-current': {'command': '', 'vals': [0, 9.4]},
#                   'device-settings:mkidarray:regulating-temp': {'command': '', 'vals': load_tvals(CALIBRATION_CURVE)}}


class MagnetCycleSettings:
    def __init__(self, redis):
        values = redis.read(redis.redis_keys('device-settings:magnet:*'))

        self.ramp_rate = values['device-settings:magnet:ramp-rate']
        self.deramp_rate = values['device-settings:magnet:deramp-rate']
        self.soak_time = values['device-settings:magnet:soak-time']
        self.soak_current = values['device-settings:magnet:soak-current']
        self.regulating_temp = values['device-settings:magnet:regulating-temp']


# COMMANDSMAGNET
COMMANDSMAGNET = {'device-settings:magnet:ramp-rate': {'command': '', 'vals': [0, 0.100]},
                  'device-settings:magnet:deramp-rate': {'command': '', 'vals': [0, 0.100]},
                  'device-settings:magnet:soak-time': {'command': '', 'vals': [0, np.inf]},
                  'device-settings:magnet:soak-current': {'command': '', 'vals': [0, 10.0]},
                  'device-settings:magnet:regulating-temp': {'command': '', 'vals': [0, 4]}}

# COMMANDSCONEX
COMMANDSCONEX = {'device-settings:conex:enabled': {'command': '', 'vals': {'Enabled': 'Enabled', 'Disabled': 'Disabled'}}}


# ---- Full command dict ----
COMMAND_DICT = {}
COMMAND_DICT.update(COMMANDS336)
COMMAND_DICT.update(COMMANDS372)
# COMMAND_DICT.update(COMMANDS960)
# COMMAND_DICT.update(COMMANDS921)
COMMAND_DICT.update(COMMANDSMAGNET)
COMMAND_DICT.update(COMMANDSHS)
COMMAND_DICT.update(COMMANDS625)
COMMAND_DICT.update(COMMANDSLASERFLIPPER)
COMMAND_DICT.update(COMMANDSFILTERWHEEL)
COMMAND_DICT.update(COMMANDSFOCUS)
COMMAND_DICT.update(COMMANDSCONEX)
