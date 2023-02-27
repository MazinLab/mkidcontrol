"""
Author: Noah Swimmer
8 October 2020

Program for communicating with and controlling the LakeShore240 Thermometry Unit.
This module is responsible for reading out 2 temperatures, that of the LN2 tank and the LHe tank.
Both are identical LakeShore DT-670A-CU diode thermometers. Using the LakeShore MeasureLink desktop application, the
LakeShore can be configured easily (it autodetects the thermometers and loads in the default calibration curve). There
will be functionality in the lakeshore240Agent to configure settings, although that should not be necessary unless the
thermometers are removed and replaced with new ones.
Again, the calibration process can be done manually using the LakeShore GUI if so desired.

HARDWARE NOTE: Ch1 -> LN2, Ch2 -> LHe

See manual in hardware/thermometry/LakeShore240_temperatureMonitor_manual.pdf
"""

import sys
import time
import logging
import threading

from mkidcontrol.devices import LakeShore240
from mkidcontrol.mkidredis import RedisError
import mkidcontrol.mkidredis as redis
import mkidcontrol.util as util


TS_KEYS = ['status:temps:lhetank', 'status:temps:ln2tank']

# STATUS_KEY = "status:device:ls240:status"

FIRMWARE_KEY = "status:device:ls240:firmware"
MODEL_KEY = 'status:device:ls240:model'
SN_KEY = 'status:device:ls240:sn'

LAKESHORE240_KEYS = list(TS_KEYS + [FIRMWARE_KEY] + [MODEL_KEY] + [SN_KEY])

QUERY_INTERVAL = 1
VALID_MODELS = ('MODEL240-2P', 'MODEL240-8P')

log = logging.getLogger()


if __name__ == "__main__":

    util.setup_logging('lakeshore240Agent')
    redis.setup_redis(ts_keys=TS_KEYS)
    lakeshore = LakeShore240(name='LAKESHORE240', port='/dev/ls240', baudrate=115200, timeout=0.1, valid_models=VALID_MODELS)

    try:
        info = lakeshore.device_info
        # Note that placing the store before exit makes this program behave differently in an abort
        #  than both of the sims, which would not alter the database. I like this better.
        redis.store({FIRMWARE_KEY: info['firmware'], MODEL_KEY: info['model'], SN_KEY: info['sn']})
    except IOError as e:
        log.error(f"When checking device info: {e}")
        redis.store({FIRMWARE_KEY: '',  MODEL_KEY: '', SN_KEY: ''})
        sys.exit(1)
    except RedisError as e:
        log.critical(f"Redis server error! {e}")
        sys.exit(1)

    while True:
        try:
            temps = lakeshore.read_temperatures()
            redis.store({'status:temps:ln2tank': temps['ln2'],
                         'status:temps:lhetank': temps['lhe']}, timeseries=True)
        except (IOError, ValueError) as e:
            log.error(f"Communication with LakeShore 240 failed: {e}")
        except RedisError as e:
            log.critical(f"Redis server error! {e}")
            sys.exit(1)
        time.sleep(QUERY_INTERVAL)
