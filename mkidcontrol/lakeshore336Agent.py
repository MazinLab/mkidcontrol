"""
Author: Noah Swimmer
21 February 2022

Program for communicating with and controlling the LakeShore336 Cryogenic Temperature Controller.

This module is responsible for reading out the non-device thermometry. This includes the intermediate stages (77K and
4K) and the 1K stage

Note that the LakeShore 336 unit does not allow for enabling/disabling channels and so whether a channel is read out (or
not) is solely dependent on the assignment of ENABLED_INPUT_CHANNELS.

N.B. Python API at https://lake-shore-python-driver.readthedocs.io/en/latest/model_336.html

TODO: Command syntax

TODO: Error handling

TODO: Logging

TODO: How to properly associate thermometer to input channel?
"""

import sys
import time
import logging
import threading
import numpy as np
from collections import defaultdict
from serial.serialutil import SerialException

from mkidcontrol.mkidredis import MKIDRedis, RedisError
from mkidcontrol.devices import LakeShoreMixin
import mkidcontrol.util as util

from lakeshore import Model336, Model336CurveHeader, Model336CurveFormat, Model336CurveTemperatureCoefficients, \
                      Model336InputSensorUnits, Model336InputSensorSettings, Model336InputSensorType, \
                      Model336RTDRange, Model336DiodeRange, Model336ThermocoupleRange

TEMP_KEYS = ['status:temps:77k-stage:temp', 'status:temps:4k-stage:temp', 'status:temps:1k-stage:temp']
SENSOR_VALUE_KEYS = ['status:temps:77k-stage:voltage', 'status:temps:4k-stage:voltage', 'status:temps:1k-stage:resistance']

TS_KEYS = TEMP_KEYS + SENSOR_VALUE_KEYS

FIRMWARE_KEY = "status:device:ls336:firmware"
MODEL_KEY = 'status:device:ls336:model'
SN_KEY = 'status:device:ls336:sn'

QUERY_INTERVAL = 10

log = logging.getLogger(__name__)
log.setLevel("DEBUG")

ENABLED_CHANNELS = ('B', 'C', 'D')  # CHANNEL ASSIGNMENTS ARE -> B:, C:, D:
ALLOWED_CHANNELS = ('A', 'B', 'C', 'D')

COMMANDSLS336 = {}
COMMANDSLS336.update({f'device-settings:ls336:input-channel-{ch.lower()}:sensor-type': {'command': 'INTYPE',
                                                                       'vals': {'DISABLED': 0, 'DIODE': 1,
                                                                                'PLATINUM_RTD': 2, 'NTC_RTD': 3,
                                                                                'THERMOCOUPLE': 4, 'CAPACITANCE': 5}} for ch in ALLOWED_CHANNELS})
COMMANDSLS336.update({f'device-settings:ls336:input-channel-{ch.lower()}:autorange-enabled': {'command': 'INTYPE',
                                                                             'vals': {'OFF': False, 'ON': True}} for ch in ALLOWED_CHANNELS})
COMMANDSLS336.update({f'device-settings:ls336:input-channel-{ch.lower()}:compensation': {'command': 'INTYPE',
                                                                        'vals': {'OFF': False, 'ON': True}} for ch in ALLOWED_CHANNELS})
COMMANDSLS336.update({f'device-settings:ls336:input-channel-{ch.lower()}:units': {'command': 'INTYPE',
                                                                 'vals': {'KELVIN': 1, 'CELSIUS': 2, 'SENSOR': 3}} for ch in ALLOWED_CHANNELS})
COMMANDSLS336.update({f'device-settings:ls336:input-channel-{ch.lower()}:input-range': {'command': 'INTYPE',
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
                                                                                'ONE_HUNDRED_THOUSAND_OHM': 8,
                                                                                'FIFTY_MILLIVOLT': 0}} for ch in ALLOWED_CHANNELS})
COMMANDSLS336.update({f'device-settings:ls336:input-channel-{ch.lower()}:curve': {'command': 'INCRV',
                                                                       'vals': np.arange(0, 60)} for ch in ALLOWED_CHANNELS})

SETTING_KEYS = tuple(COMMANDSLS336.keys())

COMMAND_KEYS = [f"command:{k}" for k in SETTING_KEYS]


def parse_ls336_command(cmd:str, val):
    setting = cmd.removeprefix("command:")
    command = COMMANDSLS336[setting]

    _, _, channel, field = setting.split(":")
    channel = channel[-1].upper()
    field = field.replace('-', '_')

    cmd_code = command['command']
    try:
        cmd_val = command['vals'][val]
    except (KeyError, IndexError):
        if int(val) in command['vals']:
            cmd_val = val
        else:
            raise ValueError(f"Invalid value ({val}) given for command {command}!")

    return channel, field, cmd_code, cmd_val


class LakeShore336(LakeShoreMixin, Model336):
    def __init__(self, name, port=None, timeout=0.1):
        """
        Initialize the LakeShore336 unit. Requires a name, typically something like 'LakeShore336' or '336'.
        The port and timeout parameters are optional. If port is none, the __init__() function from the Model 336 super
        class will search the device tree for units which have the correct PID/VID combination. If timeout is none, it
        will default to 0.1 seconds, which is lower than the default of 2 seconds in the superclass.
        """
        self.device_serial = None  # Creates a class attribute called device_serial. This will be overwritten by the
        # instance inherited from the superclass, but for clarity in what attributes are available, we initialize here.
        self.enabled_input_channels = ENABLED_CHANNELS  # Create a class attribute which is the tuple of enabled
        # channels to be used in the LakeShoreMixin class

        if port is None:
            super().__init__(timeout=timeout)
        else:
            super().__init__(com_port=port, timeout=timeout)
        self.name = name

    def modify_input_sensor(self, channel: (str, int), command_code, **desired_settings):
        """
        Reads in the current settings of the input sensor at channel <channel>, changes any setting passed as an
        argument that is not 'None', and stores the modified dict of settings in dict(new_settings). Then reads the
        new_settings dict into a Model336InputSettings object and sends the appropriate command to update the input
        settings for that channel
        """
        settings = self.query_settings(command_code, channel)

        new_settings = {}
        for k in settings.keys():
            try:
                new_settings[k] = desired_settings[k]
            except KeyError:
                new_settings[k] = settings[k]

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
        # self.set_input_sensor(channel=channel_num, sensor_parameters=settings) # TODO: Error handling


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

    def callback(tvals, svals):
        vals = tvals + svals
        keys = TEMP_KEYS + SENSOR_VALUE_KEYS
        # d = {k: x for k, x in zip(keys, vals) if x}
        d = {k: x for k, x in zip(keys, vals)}
        redis.store(d, timeseries=True)

    lakeshore.monitor(QUERY_INTERVAL, (lakeshore.temp, lakeshore.sensor_vals), value_callback=callback)

    try:
        while True:
            for key, val in redis.listen(COMMAND_KEYS):
                log.info(f"heard {key} -> {val}!")
                print(parse_ls336_command(key, val))
    except RedisError as e:
        log.error(f"Redis server error! {e}")

    print('go')
