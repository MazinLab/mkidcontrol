"""
Author: Noah Swimmer, 28 October 2022

Code to control the Finger Lakes Instrumentation (FLI) CFW2-7 Filter wheel

TODO: Add forked github repos to the mkidcontrol repository for recreation of FLI software
"""

import logging
import sys

import mkidcontrol.mkidredis as redis
import mkidcontrol.util as util
from mkidcontrol.mkidredis import RedisError
from mkidcontrol.devices import FilterWheel
from mkidcontrol.commands import COMMANDSFILTERWHEEL, LakeShoreCommand

logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger(__name__)


QUERY_INTERVAL = 1

STATUS_KEY = "status:device:filterwheel:status"
SN_KEY = "status:device:filterwheel:sn"
MODEL_KEY = "status:device:filterwheel:model"

SETTING_KEYS = tuple(COMMANDSFILTERWHEEL.keys())
COMMAND_KEYS = (f"command:{key}" for key in SETTING_KEYS)

FILTERWHEEL_POSITION_KEY = 'device-settings:filterwheel:position'
FILTERWHEEL_FILTER_KEY = 'status:filterwheel:filter'

FILTERS = {0: 'Closed',
           1: 'Y',
           2: 'Zs',
           3: 'J',
           4: '220+125',
           5: '125',
           6: 'Open'}

def callback(filter, position):
    vals = [filter, position]
    keys = [FILTERWHEEL_FILTER_KEY, FILTERWHEEL_POSITION_KEY]
    d = {k: x for k, x in zip(keys, vals)}
    try:
        if all(i is None for i in vals):
            redis.store({STATUS_KEY: "Error"})
        else:
            redis.store(d)
            redis.store({STATUS_KEY: "OK"})
    except RedisError:
        log.warning('Storing filter wheel data to redis failed!')


if __name__ == "__main__":

    redis.setup_redis()
    util.setup_logging('filterwheelAgent')

    fw = FilterWheel('filterwheel', b'/dev/filterwheel')