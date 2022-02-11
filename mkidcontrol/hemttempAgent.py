"""
Author: Noah Swimmer, 1 April 2020

Program for controlling ArduinoMEGA that monitors the bias voltages and currents of the transistors
of the cryogenic HEMT amplifiers in the PITCURE-C cryostat. Will log the values directly to redis database
for the fridge monitor to store/determine that the amplifiers are working properly.

TODO: Add HEMT rack temperature reporting (after rack thermometers installed)
"""
import sys
import numpy as np
import time

from mkidcontrol.devices import Hemtduino
from mkidcontrol.pcredis import PCRedis, RedisError
import mkidcontrol.util as util


DEVICE = "/dev/hemtduino"
QUERY_INTERVAL = 1

HEMT_VALUES = ['gate-voltage-bias', 'drain-current-bias', 'drain-voltage-bias']
KEYS = [f"status:feedline{5-i}:hemt:{j}" for i in range(5) for j in HEMT_VALUES]
KEY_DICT = {msg_idx: key for (msg_idx, key) in zip(np.arange(0, 15, 1), KEYS)}
STATUS_KEY = "status:device:hemtduino:status"
FIRMWARE_KEY = "status:device:hemtduino:firmware"

HEMTTEMP_KEYS = KEYS + [STATUS_KEY, FIRMWARE_KEY]

if __name__ == "__main__":

    log= util.setup_logging('hemttempAgent')

    redis = PCRedis(create_ts_keys=KEYS)
    hemtduino = Hemtduino(port=DEVICE, baudrate=115200, timeout=0.1)

    try:
        firmware = hemtduino.firmware
        redis.store({FIRMWARE_KEY: firmware})
        if not hemtduino.firmware_ok():
            redis.store({STATUS_KEY: 'Unsupported firmware'})
            sys.exit(1)
    except IOError:
        redis.store({FIRMWARE_KEY: ''})
        redis.store({STATUS_KEY: 'FAILURE to poll firmware'})
        sys.exit(1)
    except RedisError as e:
        log.critical(f"Redis server error! {e}")
        sys.exit(1)

    while True:
        try:
            redis.store({key: v for key, v in zip(KEYS, hemtduino.read_hemt_data())}, timeseries=True)
            redis.store({STATUS_KEY: 'OK'})
        except RedisError as e:
            log.error(f"Redis error {e}")
            sys.exit(1)
        except IOError as e:
            log.error(f"Error {e}")
            redis.store({STATUS_KEY: f"Error: {e}"})

        time.sleep(QUERY_INTERVAL)
