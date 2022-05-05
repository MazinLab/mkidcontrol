"""
Author: Noah Swimmer
21 February 2022

Program for communicating with and controlling the LakeShore625 Superconducting Magnet Power Supply.

This module is responsible for reading out

"""

import sys
import time
import logging
import threading

from mkidcontrol.devices import LakeShoreDevice
from mkidcontrol.pcredis import PCRedis, RedisError
import mkidcontrol.util as util

COMMANDS625 = {'device-settings:ls625:baud-rate': {'command': 'BAUD', 'vals': {'9600': '0', '19200': '1',
                                                                               '38400': '2', '57600': '3'}},
               'device-settings:ls625:current-output-limit': {'command': 'LIMIT', 'vals': [0.0, 60.1000]},
               'device-settings:ls625:voltage-output-limit': {'command': 'LIMIT', 'vals': [0.1000, 5.0000]},
               'device-settings:ls625:rate-output-limit': {'command': 'LIMIT', 'vals': [0.0001, 99.999]},
               'device-settings:ls625:magnetic-field-parameter': {'command': 'FLDS 1,', 'vals': [0.0100, 10.000]},
               'device-settings:ls625:quench-parameter': {'command': 'QNCH 1,', 'vals': [0.0100, 10.000]},
               'device-settings:ls625:ramp-rate': {'command': 'RATE', 'vals': [0.0001, 99.999]},
               'device-settings:ls625:desired-current': {'command': 'SETI', 'vals': [0.0000, 60.1000]},
               'device-settings:ls625:compliance-voltage': {'command': 'SETV', 'vals': [0.1000, 5.0000]},
               'device-settings:ls625:stop-current-ramp': {'command': 'STOP', 'vals': ''},
               'device-settings:ls625:control-mode': {'command': 'XPGM', 'vals': {'internal': '0', 'external':'1'}}
               }

DEVICE = '/dev/ls625'
QUERY_INTERVAL = 1
VALID_MODELS = ('MODEL625')

SETTING_KEYS = tuple(COMMANDS625.keys())

# TEMP_KEY = 'status:temps:mkidarray:temp'
# RES_KEY = 'status:temps:mkidarray:resistance'
# OUTPUT_VOLTAGE_KEY = 'status:device:sim921:sim960-vout'
# TS_KEYS = [TEMP_KEY, RES_KEY, OUTPUT_VOLTAGE_KEY]
#
# REGULATION_TEMP_KEY = "device-settings:mkidarray:regulating-temp"
# CALIBRATION_CURVE_KEY = 'device-settings:sim921:curve-number'
# TEMP_SEPOINT_KEY = 'device-settings:sim921:temp-offset'
# RES_SETPOINT_KEY = 'device-settings:sim921:resistance-offset'
#
# OUTPUT_MODE_KEY = 'device-settings:sim921:output-mode'
# OUTPUT_MODE_COMMAND_KEY = f"command:{OUTPUT_MODE_KEY}"

STATUS_KEY = "status:device:ls625:status"
FIRMWARE_KEY = "status:device:ls625:firmware"
MODEL_KEY = 'status:device:ls625:model'
SN_KEY = 'status:device:ls625:sn'

log = logging.getLogger()


class LakeShore625(LakeShoreDevice):
    def __init__(self, name, port, baudrate=9600, timeout=0.1, connect=True, valid_models=None, initializer=None):
        super().__init__(name, port, baudrate, timeout, connect=connect, valid_models=valid_models, initializer=initializer)

        if connect:
            self.connect(raise_errors=False)

    def current(self):
        current = self.query("RDGI?")
        self.last_current_read = current
        return current

# if __name__ == "__main__":
#     util.setup_logging('sim921Agent')
#     redis.setup_redis(create_ts_keys=TS_KEYS)
#     lakeshore = LakeShore625(port=DEVICE, valid_models=VALID_MODELS, initializer=initializer)
#
#     # ---------------------------------- MAIN OPERATION (The eternal loop) BELOW HERE ----------------------------------
#     # TODO
#     def callback(t, r, v):
#         d = {k: x for k, x in zip((TEMP_KEY, RES_KEY, OUTPUT_VOLTAGE_KEY), (t, r, v)) if x}
#         redis.store(d, timeseries=True)
#     sim.monitor(QUERY_INTERVAL, (sim.temp, sim.resistance, sim.output_voltage), value_callback=callback)
#
#     while True:
#         try:
#             for key, val in redis.listen(COMMAND_KEYS):
#                 log.debug(f"sim921agent received {key}, {val}. Trying to send a command.")
#                 key = key.removeprefix('command:')
#                 if key in SETTING_KEYS:
#                     try:
#                         cmd = SimCommand(key, val)
#                     except ValueError as e:
#                         log.warning(f"Ignoring invalid command ('{key}={val}'): {e}")
#                         continue
#                     try:
#                         log.info(f"Processing command '{cmd}'")
#                         sim.send(cmd.sim_string)
#                         redis.store({cmd.setting: cmd.value})
#                         redis.store({STATUS_KEY: "OK"})
#                     except IOError as e:
#                         redis.store({STATUS_KEY: f"Error {e}"})
#                         log.error(f"Comm error: {e}")
#                 elif key == REGULATION_TEMP_KEY:
#                     temp = float(val)
#                     curve = int(redis.read(CALIBRATION_CURVE_KEY))
#                     res = sim.convert_temperature_to_resistance(temp, curve)
#                     if res:
#                         t_cmd = SimCommand(TEMP_SEPOINT_KEY, temp)
#                         r_cmd = SimCommand(RES_SETPOINT_KEY, res)
#                         try:
#                             sim.send(t_cmd.sim_string)
#                             redis.store({t_cmd.setting: t_cmd.value})
#                             sim.send(r_cmd.sim_string)
#                             redis.store({r_cmd.setting: r_cmd.value})
#                             redis.store({STATUS_KEY: "OK"})
#                         except IOError as e:
#                             redis.store({STATUS_KEY: f"Error {e}"})
#                             log.error(f"Comm error: {e}")
#                     else:
#                         pass
#
#         except RedisError as e:
#             log.critical(f"Redis server error! {e}")
#             sys.exit(1)
