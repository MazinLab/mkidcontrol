"""
Author: Noah Swimmer
21 February 2022

Program for communicating with and controlling the LakeShore336 Cryogenic Temperature Controller.

This module is responsible for reading out the non-device thermometry. This includes the intermediate stages (50K and
4K) and the 1K stage

N.B. Python API at https://lake-shore-python-driver.readthedocs.io/en/latest/model_336.html

TODO: Command syntax

TODO: Error handling
"""

import sys
import time
import logging
import threading
import numpy as np
from serial.serialutil import SerialException

from mkidcontrol.mkidredis import MKIDRedis, RedisError
import mkidcontrol.util as util

from lakeshore import Model336,  Model336CurveHeader, Model336CurveFormat, Model336CurveTemperatureCoefficients, \
                      Model336InputSensorUnits, Model336InputSensorSettings, Model336InputSensorType, \
                      Model336RTDRange, Model336DiodeRange, Model336ThermocoupleRange

TEMP_KEYS = ['status:temps:50k-stage:temp', 'status:temps:3k-stage:temp', 'status:temps:1k-stage:temp']
SENSOR_VALUE_KEYS = ['status:temps:50k-stage:resistance', 'status:temps:3k-stage:voltage', 'status:temps:1k-stage:voltage']

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


class LakeShore336(Model336):
    def __init__(self, name, port=None, timeout=0.1):
        self.device_serial = None
        if port is None:
            super().__init__(timeout=timeout)
        else:
            super().__init__(com_port=port, timeout=timeout)
        self.name = name

    def disconnect(self):
        if self.device_serial:
            self.device_serial.close()

    def connect(self):
        try:
            self.device_serial.open()
        except (IOError, AttributeError) as e:
            log.warning(f"Unable to open serial port: {e}")
            raise Exception(f"Unable to open serial port: {e}")
        except SerialException as e:
            log.debug(f"Serial port is already open: {e}")

    @property
    def device_info(self):
        return dict(model=self.model_number, firmware=self.firmware_version, sn=self.serial_number)

    def read_temperatures_and_sensor_values(self):
        temp_vals = []
        sensor_vals = []
        for channel in ENABLED_CHANNELS:
            try:
                temp_vals.append(float(self.get_kelvin_reading(channel)))
                sensor_vals.append(float(self.get_sensor_reading(channel)))
            except IOError as e:
                log.error(f"Serial error: {e}")
                raise IOError(f"Serial error: {e}")
        # t_rdgs = {ENABLED_CHANNELS[i]: temp_vals[i] for i in range(len(ENABLED_CHANNELS))}
        # s_rdgs = {ENABLED_CHANNELS[i]: sensor_vals[i] for i in range(len(ENABLED_CHANNELS))}
        # return t_rdgs, s_rdgs
        return temp_vals + sensor_vals

    def sensor_settings(self, channel):
        """
        Returns a parsed dictionary
        """
        try:
            sensor_data = self.get_input_sensor(str(channel))
        except IOError as e:
            raise IOError(f"Serial error communicating with Lake Shore 336: {e}")
        except ValueError as e:
            raise ValueError(f"{channel} is not an allowed channel for the Lake Shore 336: {e}")

        return {'sensor_type': sensor_data.sensor_type,
                'autorange_enable': sensor_data.autorange_enable,
                'compensation': sensor_data.compensation,
                'units': sensor_data.units,
                'input_range': sensor_data.input_range}

    def modify_input_sensor(self, channel, sensor_type=None, autorange_enable=None,
                            compensation=None, units=None, input_range=None):

        settings = self.sensor_settings(channel)

        new_settings = {'sensor_type': sensor_type, 'autorange_enable': autorange_enable,
                        'compensation': compensation, 'units': units,'input_range': input_range}

        for k, c, n in zip(new_settings.keys(), settings.values(), new_settings.values()):
            try:
                cval = c.value
            except AttributeError:
                cval = c
            if (n != cval) and n is not None:
                log.info(f"Changing {k} from {cval} -> {n} for channel {channel}")
                new_settings[k] = n
            else:
                new_settings[k] = c

        if new_settings['sensor_type'] == 0:
            new_settings['input_range'] = None
        elif new_settings['sensor_type'] == 1:
            new_settings['input_range'] = Model336DiodeRange(new_settings['input_range'])
        elif new_settings['sensor_type'] in (2, 3):
            new_settings['input_range'] = Model336RTDRange(new_settings['input_range'])
        elif new_settings['sensor_type'] == 4:
            new_settings['input_range'] = Model336ThermocoupleRange(new_settings['input_range'])
        else:
            raise ValueError(f"{new_settings['input_range']} is not an allowed value!")

        settings = Model336InputSensorSettings(sensor_type=Model336InputSensorType(new_settings['sensor_type']),
                                               autorange_enable=new_settings['autorange_enable'],
                                               compensation=new_settings['compensation'],
                                               units=Model336InputSensorUnits(new_settings['units']),
                                               input_range=new_settings['input_range'])
        return settings
        # self.set_input_sensor(channel=channel_num, sensor_parameters=settings)

    def curve_settings(self, curve_num):
        try:
            curve_header = self.get_curve_header(curve_num)
        except IOError as e:
            raise IOError(f"Serial error communicating with Lake Shore 336: {e}")
        except ValueError as e:
            raise ValueError(f"{curve_num} is not an allowed curve number for the Lake Shore 336: {e}")

        return {'curve_name': curve_header.curve_name,
                'serial_number': curve_header.serial_number,
                'curve_data_format': curve_header.curve_data_format,
                'temperature_limit': curve_header.temperature_limit,
                'coefficient': curve_header.coefficient}

    def modify_curve_header(self, curve_num, curve_name=None, serial_number=None, curve_data_format=None,
                            temperature_limit=None, coefficient=None):

        settings = self.curve_settings(curve_num)

        new_settings = {'curve_name': curve_name, 'serial_number': serial_number,
                        'curve_data_format': curve_data_format, 'temperature_limit': temperature_limit,
                        'coefficient': coefficient}

        for k, c, n in zip(new_settings.keys(), settings.values(), new_settings.values()):
            try:
                cval = c.value
            except AttributeError:
                cval = c
            if (n != cval) and n is not None:
                log.info(f"Changing {k} from {cval} -> {n} for curve {curve_num}")
                new_settings[k] = n
            else:
                new_settings[k] = c

        header = Model336CurveHeader(curve_name=new_settings['curve_name'],
                                     serial_number=new_settings['serial_number'],
                                     curve_data_format=Model336CurveFormat(new_settings['curve_data_format']),
                                     temperature_limit=new_settings['temperature_limit'],
                                     coefficient=Model336CurveTemperatureCoefficients(new_settings['coefficient']))

        return header
        # self.set_curve_header(curve_number=curve_num, curve_header=header)

    def load_curve_data(self, curve_num, data=None, data_file=None):
        """
        Curve_num is the desired curve to load data into. Valid options are 21-59.

        If data_file is not none, loads the data from the given .txt file on the system (there is not current support
        for files of other formats such as .npz) . The expected format is 2 columns, column 0 is the sensor values and
        column 1 is the associated calibrated temperature values. The temeprature values should always run high to low.
        If data is not none, it is understood that the user is passing the data directly to the function. The format for
        the data should be the same as in the description for the data_file.
        data_file will take priority over data.

        # TODO: Format checking
        """
        if data:
            curve_data = data
        elif data_file:
            curve_data = np.loadtxt(data_file)
        else:
            raise ValueError(f"No data supplied to load to the curve")

        self.set_curve(curve_num, curve_data)

    def change_curve(self, channel, curve_num):
        current_curve = self.get_input_curve(channel)
        if current_curve != curve_num:
            log.info(f"Changing curve for input channel {channel} from {current_curve} to {curve_num}")
            self.set_input_curve(channel, curve_num)
        else:
            log.warning(f"Requested to set channel {channel}'s curve from {current_curve} to {curve_num}, no change"
                        f"sent to Lake Shore 336.")

    def monitor(self, interval: float, monitor_func: (callable, tuple), value_callback: (callable, tuple) = None):
        """
        Given a monitoring function (or is of the same) and either one or the same number of optional callback
        functions call the monitors every interval. If one callback it will get all the values in the order of the
        monitor funcs, if a list of the same number as of monitorables each will get a single value.

        Monitor functions may not return None.

        When there is a 1-1 correspondence the callback is not called in the event of a monitoring error.
        If a single callback is present for multiple monitor functions values that had errors will be sent as None.
        Function must accept as many arguments as monitor functions.
        """
        if not isinstance(monitor_func, (list, tuple)):
            monitor_func = (monitor_func,)
        if value_callback is not None and not isinstance(value_callback, (list, tuple)):
            value_callback = (value_callback,)
        if not (value_callback is None or len(monitor_func) == len(value_callback) or len(value_callback) == 1):
            raise ValueError('When specified, the number of callbacks must be one or the number of monitor functions')

        def f():
            while True:
                vals = []
                for func in monitor_func:
                    try:
                        vals.append(func())
                    except IOError as e:
                        log.error(f"Failed to poll {func}: {e}")
                        vals.append(None)

                if value_callback is not None:
                    if len(value_callback) > 1 or len(monitor_func) == 1:
                        for v, cb in zip(vals, value_callback):
                            if v is not None:
                                try:
                                    cb(v)
                                except Exception as e:
                                    log.error(f"Callback {cb} error. arg={v}.", exc_info=True)
                    else:
                        cb = value_callback[0]
                        try:
                            cb(*vals)
                        except Exception as e:
                            log.error(f"Callback {cb} error. args={vals}.", exc_info=True)

                time.sleep(interval)

        self._monitor_thread = threading.Thread(target=f, name='Monitor Thread')
        self._monitor_thread.daemon = True
        self._monitor_thread.start()


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
