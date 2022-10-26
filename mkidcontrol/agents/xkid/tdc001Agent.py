"""
Author: Noah Swimmer, 26 October 2022

Agent for controlling the
"""

from thorlabs_apt_device.devices.tdc001 import TDC001
from mkidcontrol.mkidredis import RedisError
import mkidcontrol.mkidredis as redis
import mkidcontrol.util as util

TS_KEYS = ()

if __name__ == "__main__":
    redis.setup_redis(ts_keys=TS_KEYS)
    util.setup_logging('tdc001Agent')

    t = TDC001(serial_port='')