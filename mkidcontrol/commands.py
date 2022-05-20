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
        return f"{self.setting}->{self.value}: {self.sim_string}"

    @property
    def is_query(self):
        return self.value is None

    @property
    def sim_string(self):
        """
        Returns the command string for the SIM.
        """
        if self.is_query:
            return self.sim_query_string

        v = self.mapping[self.value] if self.range is None else self.value
        return f"{self.command} {v}"

    @property
    def sim_query_string(self):
        """ Returns the corresponding command string to query for the setting"""
        return f"{self.command}?"


class LakeShoreCommand:
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
        return f"{self.setting}->{self.value}: {self.sim_string}"

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


def load_tvals(curve):
    if curve == 1:
        file = '/home/mazinlab/picturec/docs/hardware_reference_documentation/thermometry/RX-102A/RX-102A_Mean_Curve.tbl'
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
ENABLED_336_CHANNELS = ('B', 'C', 'D')
ALLOWED_336_CHANNELS = ("A", "B", "C", "D")

COMMANDS336 = {}
COMMANDS336.update({f'device-settings:ls336:input-channel-{ch.lower()}:sensor-type': {'command': 'INTYPE',
                                                                       'vals': {'DISABLED': 0, 'DIODE': 1,
                                                                                'PLATINUM_RTD': 2, 'NTC_RTD': 3}} for ch in ALLOWED_336_CHANNELS})
COMMANDS336.update({f'device-settings:ls336:input-channel-{ch.lower()}:autorange-enabled': {'command': 'INTYPE',
                                                                             'vals': {'OFF': False, 'ON': True}} for ch in ALLOWED_336_CHANNELS})
COMMANDS336.update({f'device-settings:ls336:input-channel-{ch.lower()}:compensation': {'command': 'INTYPE',
                                                                        'vals': {'OFF': False, 'ON': True}} for ch in ALLOWED_336_CHANNELS})
COMMANDS336.update({f'device-settings:ls336:input-channel-{ch.lower()}:units': {'command': 'INTYPE',
                                                                 'vals': {'KELVIN': 1, 'CELSIUS': 2, 'SENSOR': 3}} for ch in ALLOWED_336_CHANNELS})
COMMANDS336.update({f'device-settings:ls336:input-channel-{ch.lower()}:input-range': {'command': 'INTYPE',
                                                                       'vals': {'TWO_POINT_FIVE_VOLTS': 0,
                                                                                'TEN_VOLTS': 1,
                                                                                'TEN_OHM': 0,
                                                                                'THIRTY_OHM': 1,
                                                                                'HUNDRED_OHM': 2,
                                                                                'THREE_HUNDRED_OHM': 3,
                                                                                'ONE_THOUSAND_OHM': 4,
                                                                                'THREE_THOUSAND_OHM': 5,
                                                                                'TEN_THOUSAND_OHM': 6,
                                                                                'THIRTY_THOUSAND_OHM': 7,
                                                                                'ONE_HUNDRED_THOUSAND_OHM': 8}} for ch in ALLOWED_336_CHANNELS})
COMMANDS336.update({f'device-settings:ls336:input-channel-{ch.lower()}:curve': {'command': 'INCRV',
                                                                       'vals': {str(cn): cn for cn in np.arange(1, 60)}} for ch in ALLOWED_336_CHANNELS})
COMMANDS336.update({f'device-settings:ls336:curve-{cu}:curve-name': {'command': 'CRVHDR', 'vals': None} for cu in np.arange(21, 60)})
COMMANDS336.update({f'device-settings:ls336:curve-{cu}:serial-number': {'command': 'CRVHDR', 'vals': None} for cu in np.arange(21, 60)})
COMMANDS336.update({f'device-settings:ls336:curve-{cu}:curve-data-format': {'command': 'CRVHDR', 'vals': {'MILLIVOLT_PER_KELVIN': 1,
                                                                                                            'VOLTS_PER_KELVIN': 2,
                                                                                                            'OHMS_PER_KELVIN': 3,
                                                                                                            'LOG_OHMS_PER_KELVIN': 4}} for cu in np.arange(21, 60)})
COMMANDS336.update({f'device-settings:ls336:curve-{cu}:temperature-limit': {'command': 'CRVHDR', 'vals': [0, 400]} for cu in np.arange(21, 60)})
COMMANDS336.update({f'device-settings:ls336:curve-{cu}:coefficient': {'command': 'CRVHDR', 'vals': {'NEGATIVE': 1,
                                                                                                      'POSITIVE': 2}} for cu in np.arange(21, 60)})

# ---- Lake Shore 372 Commands ----
ENABLED_372_INPUT_CHANNELS = ("A")
ALLOWED_372_INPUT_CHANNELS = ("A", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12", "13", "14", "15", "16")
ENABLED_372_OUTPUT_CHANNELS = (0, )
ALLOWED_372_OUTPUT_CHANNELS = (0, 1, 2)

COMMANDS372 = {}
COMMANDS372.update({f'device-settings:ls372:input-channel-{ch.lower()}:mode': {'command': 'INTYPE', 'vals': {'VOLTAGE': 0, 'CURRENT': 1}} for ch in ALLOWED_372_INPUT_CHANNELS})
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
 'RANGE_31_POINT_6_MILLI_AMPS': 22}} for ch in ALLOWED_372_INPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:input-channel-{ch.lower()}:auto-range': {'command': 'INTYPE', 'vals': {'OFF': 0, 'CURRENT': 1, 'ROX102B': 2}} for ch in ALLOWED_372_INPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:input-channel-{ch.lower()}:current-source-shunted': {'command': 'INTYPE', 'vals': {'EXCITATION OFF': False, 'EXCITATION ON': True}} for ch in ALLOWED_372_INPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:input-channel-{ch.lower()}:units': {'command': 'INTYPE', 'vals': {'KELVIN': 1, 'OHMS': 2}} for ch in ALLOWED_372_INPUT_CHANNELS})
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
 'RANGE_63_POINT_2_MEGA_OHMS': 22}} for ch in ALLOWED_372_INPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:input-channel-{ch.lower()}:enable': {'command': 'INSET', 'vals': {'ENABLED': True, 'DISABLED': False}} for ch in ALLOWED_372_INPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:input-channel-{ch.lower()}:dwell-time': {'command': 'INSET', 'vals': [0, 200]} for ch in ALLOWED_372_INPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:input-channel-{ch.lower()}:pause-time': {'command': 'INSET', 'vals': [3, 200]} for ch in ALLOWED_372_INPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:input-channel-{ch.lower()}:curve-number': {'command': 'INSET', 'vals': {str(cn): cn for cn in np.arange(1,60)}} for ch in ALLOWED_372_INPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:input-channel-{ch.lower()}:temperature-coefficient': {'command': 'INSET', 'vals': {'NEGATIVE': 1, 'POSITIVE': 2}} for ch in ALLOWED_372_INPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:heater-channel-{ch}:output-mode': {'command': 'OUTMODE', 'vals': {'OFF': 0,
 'MONITOR_OUT': 1,
 'OPEN_LOOP': 2,
 'ZONE': 3,
 'STILL': 4,
 'CLOSED_LOOP': 5,
 'WARMUP': 6}} for ch in ALLOWED_372_OUTPUT_CHANNELS})
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
 'CONTROL': 'A'}} for ch in ALLOWED_372_OUTPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:heater-channel-{ch}:powerup-enable': {'command': 'OUTMODE', 'vals': {'ENABLED': True, 'DISABLED': False}} for ch in ALLOWED_372_OUTPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:heater-channel-{ch}:reading-filter': {'command': 'OUTMODE', 'vals': {'ENABLED': True, 'DISABLED': False}} for ch in ALLOWED_372_OUTPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:heater-channel-{ch}:delay': {'command': 'OUTMODE', 'vals': [1, 255]} for ch in ALLOWED_372_OUTPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:heater-channel-{ch}:polarity': {'command': 'OUTMODE', 'vals': {'UNIPOLAR': 0, 'BIPOLAR': 1}} for ch in ALLOWED_372_OUTPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:heater-channel-{ch}:setpoint': {'command': 'SETP', 'vals': [0, 4]} for ch in ALLOWED_372_OUTPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:heater-channel-{ch}:gain': {'command': 'PID', 'vals': [0, 1000]} for ch in ALLOWED_372_OUTPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:heater-channel-{ch}:integral': {'command': 'PID', 'vals': [0, 10000]} for ch in ALLOWED_372_OUTPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:heater-channel-{ch}:ramp_rate': {'command': 'PID', 'vals': [0, 2500]} for ch in ALLOWED_372_OUTPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:heater-channel-{ch}:range': {'command': 'RANGE', 'vals': {'OFF': 0, 'ON': True,
 'RANGE_31_POINT_6_MICRO_AMPS': 1,
 'RANGE_100_MICRO_AMPS': 2,
 'RANGE_316_MICRO_AMPS': 3,
 'RANGE_1_MILLI_AMP': 4,
 'RANGE_3_POINT_16_MILLI_AMPS': 5,
 'RANGE_10_MILLI_AMPS': 6,
 'RANGE_31_POINT_6_MILLI_AMPS': 7,
 'RANGE_100_MILLI_AMPS': 8}} for ch in ALLOWED_372_OUTPUT_CHANNELS})
COMMANDS372.update({f'device-settings:ls372:curve-{cu}:curve-name': {'command': 'CRVHDR', 'vals': None} for cu in np.arange(21, 60)})
COMMANDS372.update({f'device-settings:ls372:curve-{cu}:serial-number': {'command': 'CRVHDR', 'vals': None} for cu in np.arange(21, 60)})
COMMANDS372.update({f'device-settings:ls372:curve-{cu}:curve-data-format': {'command': 'CRVHDR', 'vals': {'MILLIVOLT_PER_KELVIN': 1,
                                                                                                            'VOLTS_PER_KELVIN': 2,
                                                                                                            'OHMS_PER_KELVIN': 3,
                                                                                                            'LOG_OHMS_PER_KELVIN': 4}} for cu in np.arange(21, 60)})
COMMANDS372.update({f'device-settings:ls372:curve-{cu}:temperature-limit': {'command': 'CRVHDR', 'vals': [0, 400]} for cu in np.arange(21, 60)})
COMMANDS372.update({f'device-settings:ls372:curve-{cu}:coefficient': {'command': 'CRVHDR', 'vals': {'NEGATIVE': 1,
                                                                                                      'POSITIVE': 2}} for cu in np.arange(21, 60)})

# ---- SRS SIM 921 Commands ----
COMMANDS921 = {'device-settings:sim921:resistance-range': {'command': 'RANG', 'vals': {'20e-3': '0', '200e-3': '1', '2': '2',
                                                                                       '20': '3', '200': '4', '2e3': '5',
                                                                                       '20e3': '6', '200e3': '7',
                                                                                       '2e6': '8', '20e6': '9'}},
               'device-settings:sim921:excitation-value': {'command': 'EXCI', 'vals': {'0': '-1', '3e-6': '0', '10e-6': '1',
                                                                                       '30e-6': '2', '100e-6': '3',
                                                                                       '300e-6': '4', '1e-3': '5',
                                                                                       '3e-3': '6', '10e-3': '7', '30e-3': '8'}},
               'device-settings:sim921:excitation-mode': {'command': 'MODE', 'vals': {'passive': '0', 'current': '1',
                                                                                      'voltage': '2', 'power': '3'}},
               'device-settings:sim921:time-constant': {'command': 'TCON', 'vals': {'0.3': '0', '1': '1', '3': '2', '10': '3',
                                                                                    '30': '4', '100': '5', '300': '6'}},
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
COMMANDSHS = {'device-settings:currentduino:heatswitch': {'command': '', 'vals': {'open': 'open', 'close': 'close'}}}

# ---- PICTURE-C Magnet Commands ----
CALIBRATION_CURVE = 1

# COMMANDS MAGNET are only included so that we can use the SimCommand class to check the legality of a magnet command.
COMMANDSMAGNET = {'device-settings:sim960:ramp-rate': {'command': '', 'vals': [0, 0.015]},
                  'device-settings:sim960:deramp-rate': {'command': '', 'vals': [-0.015, 0]},
                  'device-settings:sim960:soak-time': {'command': '', 'vals': [0, np.inf]},
                  'device-settings:sim960:soak-current': {'command': '', 'vals': [0, 9.4]},
                  'device-settings:mkidarray:regulating-temp': {'command': '', 'vals': load_tvals(CALIBRATION_CURVE)}}

# ---- Full command dict ----
COMMAND_DICT = {}
# COMMAND_DICT.update(COMMANDS336)
# COMMAND_DICT.update(COMMANDS372)
COMMAND_DICT.update(COMMANDS960)
COMMAND_DICT.update(COMMANDS921)
COMMAND_DICT.update(COMMANDSHS)
COMMAND_DICT.update(COMMANDSMAGNET)

LAKESHORE_COMMAND_DICT = {}
LAKESHORE_COMMAND_DICT.update(COMMANDS336)
LAKESHORE_COMMAND_DICT.update(COMMANDS372)