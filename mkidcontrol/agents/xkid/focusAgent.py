"""
Author: Noah Swimmer, 26 October 2022

Agent for controlling the Thorlabs TDC001 motor slider which will control the focus position

TODO: All
"""

from thorlabs_apt_device.devices.tdc001 import TDC001
from mkidcontrol.mkidredis import RedisError
import mkidcontrol.mkidredis as redis
import mkidcontrol.util as util

TS_KEYS = ()


class Focus(TDC001):
    def __init__(self, name, port=None):
        self.name = name


if __name__ == "__main__":
    redis.setup_redis(ts_keys=TS_KEYS)
    util.setup_logging('tdc001Agent')

    f = Focus(port='/dev/focus')