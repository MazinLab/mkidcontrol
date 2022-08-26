"""
Author: Noah Swimmer 15 June 2020

Program to control ArduinoUNO that will measure the current through the ADR magnet by monitoring the
current-sensing resistor on the PIPER-designed HighCurrent Boost board (see picturec reference folder
for circuit drawing). Will log values to redis, will also act as a safeguard to tell the magnet current
control that the current is operating out of normal bounds.
NOTE: Redis/redistimeseries MUST be set up for the currentduino to work.

TODO: Test the heat switch touch signals to have an open/closed monitor (in lab)
"""

import sys
import time
import logging
import threading

from mkidcontrol.devices import Currentduino, HeatswitchPosition
import mkidcontrol.mkidredis as redis
from mkidcontrol.mkidredis import RedisError
import mkidcontrol.util as util


QUERY_INTERVAL = .1

DEVICE = '/dev/currentduino'

KEYS = ['device-settings:currentduino:heatswitch',
        'status:highcurrentboard:current']

STATUS_KEY = "status:device:currentduino:status"
FIRMWARE_KEY = "status:device:currentduino:firmware"
HEATSWITCH_STATUS_KEY = 'device-settings:currentduino:heatswitch'
HEATSWITCH_MOVE_KEY = f'command:{HEATSWITCH_STATUS_KEY}'
CURRENT_VALUE_KEY = 'status:highcurrentboard:current'

COMMAND_KEYS = [HEATSWITCH_MOVE_KEY]
CURRENTDUINO_KEYS = KEYS + [STATUS_KEY, FIRMWARE_KEY, HEATSWITCH_STATUS_KEY, CURRENT_VALUE_KEY]

log = logging.getLogger(__name__)


def close():
    redis.publish(HEATSWITCH_MOVE_KEY, HeatswitchPosition.CLOSE, store=False)


def open():
    redis.publish(HEATSWITCH_MOVE_KEY, HeatswitchPosition.OPEN, store=False)


def is_opened():
    return redis.read(HEATSWITCH_STATUS_KEY) == HeatswitchPosition.OPEN


def is_closed():
    return redis.read(HEATSWITCH_STATUS_KEY) == HeatswitchPosition.CLOSE



if __name__ == "__main__":

    util.setup_logging('currentduinoAgent')

    redis.setup_redis(create_ts_keys=CURRENT_VALUE_KEY)
    currentduino = Currentduino(port=DEVICE, baudrate=115200, timeout=0.1)

    try:
        firmware = currentduino.firmware
        redis.store({FIRMWARE_KEY: firmware})
        if not currentduino.firmware_ok():
            redis.store({STATUS_KEY: 'Unsupported firmware'})
            sys.exit(1)
    except IOError:
        redis.store({FIRMWARE_KEY: ''})
        redis.store({STATUS_KEY: 'FAILURE to poll firmware'})
        sys.exit(1)
    except RedisError as e:
        log.critical(f"Redis server error! {e}")
        sys.exit(1)

    store_func = lambda x: redis.store({CURRENT_VALUE_KEY: x}, timeseries=True)
    currentduino.monitor_current(QUERY_INTERVAL, value_callback=store_func)

    while True:
        try:
            for key, val in redis.listen(COMMAND_KEYS):
                hspos = val.lower()
                try:
                    currentduino.move_heat_switch(hspos)
                    time.sleep(1)
                    if currentduino.check_hs_pos(hspos):
                        redis.store({HEATSWITCH_STATUS_KEY: hspos})
                    else:
                        pass
                except IOError as e:
                    log.info(f"Some error communicating with the arduino! {e}")
                except ValueError as e:
                    log.info(f"An invalid value was sent to the arduino. "
                             f"Please check to make sure your program is sending valid heatswitch positions.")
        except RedisError as e:
            log.critical(f"Redis server error! {e}")
            break
