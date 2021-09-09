"""
Author: Noah Swimmer, 8 July 2020

Program for communicating with and controlling the SIM921 AC resistance bridge. The primary function of the SIM921
is monitoring the temperature of the thermometer on the MKID device stage in the PICTURE-C cryostat. It is also
responsible for properly conditioning its output signal so that the SIM960 (PID Controller) can properly regulate
the device temperature.
"""
import logging
import sys
import time
from picturec.pcredis import RedisError
import picturec.util as util
from picturec.devices import SIM921, SimCommand, SIM921OutputMode, COMMANDS921
import picturec.pcredis as redis


DEVICE = '/dev/sim921'
QUERY_INTERVAL = 1

SETTING_KEYS = tuple(COMMANDS921.keys())


TEMP_KEY = 'status:temps:mkidarray:temp'
RES_KEY = 'status:temps:mkidarray:resistance'
OUTPUT_VOLTAGE_KEY = 'status:device:sim921:sim960-vout'
TS_KEYS = [TEMP_KEY, RES_KEY, OUTPUT_VOLTAGE_KEY]

REGULATION_TEMP_KEY = "device-settings:mkidarray:regulating-temp"
CALIBRATION_CURVE_KEY = 'device-settings:sim921:curve-number'
TEMP_SEPOINT_KEY = 'device-settings:sim921:temp-offset'
RES_SETPOINT_KEY = 'device-settings:sim921:resistance-offset'

OUTPUT_MODE_KEY = 'device-settings:sim921:output-mode'
OUTPUT_MODE_COMMAND_KEY = f"command:{OUTPUT_MODE_KEY}"

STATUS_KEY = 'status:device:sim921:status'
MODEL_KEY = 'status:device:sim921:model'
FIRMWARE_KEY = 'status:device:sim921:firmware'
SN_KEY = 'status:device:sim921:sn'

SIM921_KEYS = TS_KEYS + [REGULATION_TEMP_KEY, CALIBRATION_CURVE_KEY, TEMP_SEPOINT_KEY, RES_SETPOINT_KEY,
                         OUTPUT_MODE_KEY, STATUS_KEY, MODEL_KEY, FIRMWARE_KEY, SN_KEY]
COMMAND_KEYS = [f"command:{k}" for k in SETTING_KEYS + (REGULATION_TEMP_KEY,)]

log = logging.getLogger(__name__)


def to_scaled_output():
    redis.publish(OUTPUT_MODE_COMMAND_KEY, SIM921OutputMode.SCALED, store=False)


def to_manual_output():
    redis.publish(OUTPUT_MODE_COMMAND_KEY, SIM921OutputMode.MANUAL, store=False)


def in_scaled_output():
    return redis.read(OUTPUT_MODE_KEY) == SIM921OutputMode.SCALED


def in_manual_output():
    return redis.read(OUTPUT_MODE_KEY) == SIM921OutputMode.MANUAL


def firmware_pull(sim):
    # Grab and store device info
    try:
        info = sim.device_info
        d = {FIRMWARE_KEY: info['firmware'], MODEL_KEY: info['model'], SN_KEY: info['sn']}
    except IOError as e:
        log.error(f"When checking device info: {e}")
        d = {FIRMWARE_KEY: '', MODEL_KEY: '', SN_KEY: ''}

    try:
        redis.store(d)
    except RedisError:
        log.warning('Storing device info to redis failed')


def initializer(sim):
    """
    Callback run on connection to the sim whenever it is not initialized. This will only happen if the sim loses all
    of its settings, which should never every happen. Any settings applied take immediate effect
    """
    firmware_pull(sim)
    try:
        settings_to_load = redis.read(SETTING_KEYS, error_missing=True)
        initialized_settings = sim.apply_schema_settings(settings_to_load)
        time.sleep(1)
    except RedisError as e:
        log.critical('Unable to pull settings from redis to initialize sim960')
        raise IOError(e)
    except KeyError as e:
        log.critical('Unable to pull setting {e} from redis to initialize sim960')
        raise IOError(e)

    try:
        redis.store(initialized_settings)
    except RedisError:
        log.warning('Storing device settings to redis failed')


if __name__ == "__main__":

    util.setup_logging('sim921Agent')
    redis.setup_redis(create_ts_keys=TS_KEYS)
    sim = SIM921(port=DEVICE, timeout=.05, initializer=initializer)

    # ---------------------------------- MAIN OPERATION (The eternal loop) BELOW HERE ----------------------------------
    def callback(t, r, v):
        d = {k: x for k, x in zip((TEMP_KEY, RES_KEY, OUTPUT_VOLTAGE_KEY), (t, r, v)) if x}
        redis.store(d, timeseries=True)
    sim.monitor(QUERY_INTERVAL, (sim.temp, sim.resistance, sim.output_voltage), value_callback=callback)

    while True:
        try:
            for key, val in redis.listen(COMMAND_KEYS):
                log.debug(f"sim921agent received {key}, {val}. Trying to send a command.")
                key = key.removeprefix('command:')
                if key in SETTING_KEYS:
                    try:
                        cmd = SimCommand(key, val)
                    except ValueError as e:
                        log.warning(f"Ignoring invalid command ('{key}={val}'): {e}")
                        continue
                    try:
                        log.info(f"Processing command '{cmd}'")
                        sim.send(cmd.sim_string)
                        redis.store({cmd.setting: cmd.value})
                        redis.store({STATUS_KEY: "OK"})
                    except IOError as e:
                        redis.store({STATUS_KEY: f"Error {e}"})
                        log.error(f"Comm error: {e}")
                elif key == REGULATION_TEMP_KEY:
                    temp = float(val)
                    curve = int(redis.read(CALIBRATION_CURVE_KEY))
                    res = sim.convert_temperature_to_resistance(temp, curve)
                    if res:
                        t_cmd = SimCommand(TEMP_SEPOINT_KEY, temp)
                        r_cmd = SimCommand(RES_SETPOINT_KEY, res)
                        try:
                            sim.send(t_cmd.sim_string)
                            redis.store({t_cmd.setting: t_cmd.value})
                            sim.send(r_cmd.sim_string)
                            redis.store({r_cmd.setting: r_cmd.value})
                            redis.store({STATUS_KEY: "OK"})
                        except IOError as e:
                            redis.store({STATUS_KEY: f"Error {e}"})
                            log.error(f"Comm error: {e}")
                    else:
                        pass

        except RedisError as e:
            log.critical(f"Redis server error! {e}")
            sys.exit(1)
