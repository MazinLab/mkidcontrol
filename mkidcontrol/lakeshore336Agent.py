"""
Author: Noah Swimmer
21 February 2022

Program for communicating with and controlling the LakeShore336 Cryogenic Temperature Controller.

This module is responsible for reading out the non-device thermometry. This includes the intermediate stages (77K and
4K) and the 1K stage

N.B. Python API at https://lake-shore-python-driver.readthedocs.io/en/latest/model_336.html

TODO: Figure out how to 'block' settings if other settings are in place (e.g. Input range cannot be in V if sensor type is RTD)

TODO: Initialization of settings on startup
"""

import sys
import logging
from serial import SerialException

from mkidcontrol.mkidredis import RedisError
from mkidcontrol.devices import LakeShoreMixin
import mkidcontrol.util as util
from mkidcontrol.commands import COMMANDS336
import mkidcontrol.mkidredis as redis

from lakeshore import Model336, Model336InputSensorUnits, Model336InputSensorSettings, Model336InputSensorType, \
    Model336RTDRange, Model336DiodeRange, Model336ThermocoupleRange

log = logging.getLogger(__name__)

ENABLED_CHANNELS = ('B', 'C', 'D')
ALLOWED_CHANNELS = ("A", "B", "C", "D")

TEMP_KEYS = ['status:temps:77k-stage:temp', 'status:temps:4k-stage:temp', 'status:temps:1k-stage:temp']
SENSOR_VALUE_KEYS = ['status:temps:77k-stage:voltage', 'status:temps:4k-stage:voltage', 'status:temps:1k-stage:resistance']

TS_KEYS = TEMP_KEYS + SENSOR_VALUE_KEYS

STATUS_KEY = 'status:device:ls336:status'
FIRMWARE_KEY = "status:device:ls336:firmware"
MODEL_KEY = 'status:device:ls336:model'
SN_KEY = 'status:device:ls336:sn'

QUERY_INTERVAL = 1

SETTING_KEYS = tuple(COMMANDS336.keys())

COMMAND_KEYS = [f"command:{k}" for k in SETTING_KEYS]


class LakeShore336Command:
    def __init__(self, schema_key, value=None):
        """
        Initializes a LakeShore336Command. Takes in a redis device-setting:* key and desired value an evaluates it for
        its type, the mapping of the command, and appropriately sets the mapping|range for the command. If the setting
        is not supported, raise a ValueError.
        """

        if schema_key not in COMMANDS336.keys():
            raise ValueError(f'Unknown command: {schema_key}')

        self.range = None
        self.mapping = None
        self.value = value
        self.setting = schema_key

        self.command = COMMANDS336[self.setting]['command']
        setting_vals = COMMANDS336[self.setting]['vals']

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

    def change_curve(self, channel, command_code, curve_num=None):
        """
        Takes in an input channel and the relevant command code from the LAKESHORE_COMMANDS dict to query what the
        current calibration curve is in use. If the curve_num given is not none or the same as the one which is already
        loaded in, it will attempt to change to a new calibration curve for that input channel
        If no curve number is given or the user tries to change to the current curve (i.e. Channel A uses Curve 2, try
        switching to curve 2), no change will be made.
        """
        current_curve = self.query_settings(command_code, channel)

        if current_curve != curve_num and curve_num is not None:
            try:
                log.info(f"Changing curve for input channel {channel} from {current_curve} to {curve_num}")
                self.set_input_curve(channel, curve_num)
            except (SerialException, IOError) as e:
                log.error(f"...failed: {e}")
                raise e

        else:
            log.warning(f"Requested to set channel {channel}'s curve from {current_curve} to {curve_num}, no change"
                     f"sent to Lake Shore {self.model_number}.")

    def modify_input_sensor(self, channel: (str, int), command_code, **desired_settings):
        """
        Reads in the current settings of the input sensor at channel <channel>, changes any setting passed as an
        argument that is not 'None', and stores the modified dict of settings in dict(new_settings). Then reads the
        new_settings dict into a Model336InputSettings object and sends the appropriate command to update the input
        settings for that channel
        """
        new_settings = self._generate_new_settings(channel=channel, command_code=command_code, **desired_settings)

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

        try:
            log.info(f"Applying new settings to channel {channel}: {settings}")
            self.set_input_sensor(channel=channel, sensor_parameters=settings)
        except (SerialException, IOError) as e:
            log.error(f"...failed: {e}")
            raise e


if __name__ == "__main__":

    util.setup_logging('lakeshore336Agent')
    redis.setup_redis(create_ts_keys=TS_KEYS)

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
        d = {k: x for k, x in zip(keys, vals) if x}
        redis.store(d, timeseries=True)

    lakeshore.monitor(QUERY_INTERVAL, (lakeshore.temp, lakeshore.sensor_vals), value_callback=callback)

    try:
        while True:
            for key, val in redis.listen(COMMAND_KEYS):
                log.debug(f"heard {key} -> {val}!")
                try:
                    cmd = LakeShore336Command(key.removeprefix('command:'), val)
                except ValueError as e:
                    log.warning(f"Ignoring invalid command ('{key}={val}'): {e}")
                    continue
                try:
                    log.info(f"Processing command '{cmd}'")
                    if cmd.command_code == "INTYPE":
                        lakeshore.modify_input_sensor(channel=cmd.channel, command_code=cmd.command_code, **cmd.desired_setting)
                    elif cmd.command_code == "INCRV":
                        lakeshore.change_curve(channel=cmd.channel, command_code=cmd.command_code, curve_num=cmd.command_value)
                    elif cmd.command_code == "CRVHDR":
                        lakeshore.modify_curve_header(curve_num=cmd.curve, command_code=cmd.command_code, **cmd.desired_setting)
                    else:
                        pass
                    redis.store({cmd.setting: cmd.value})
                    redis.store({STATUS_KEY: "OK"})
                except IOError as e:
                    redis.store({STATUS_KEY: f"Error {e}"})
                    log.error(f"Comm error: {e}")
    except RedisError as e:
        log.critical(f"Redis server error! {e}")
        sys.exit(1)
