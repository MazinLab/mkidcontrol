import numpy as np

import mkidcontrol.mkidredis as redis
import time
import datetime

TS_KEYS = ['status:temps:50k-stage:temp',
           'status:temps:50k-stage:voltage',
           'status:temps:3k-stage:temp',
           'status:temps:3k-stage:voltage',
           'status:temps:1k-stage:temp',
           'status:temps:1k-stage:resistance',
           'status:temps:device-stage:temp',
           'status:temps:device-stage:resistance']


if __name__=="__main__":

    header = ['Time', 'Timestamp', '50k T', '50k V', '3k T', '3k V', '1k T', '1k R', 'Device T', 'Device R']

    redis.setup_redis(ts_keys=TS_KEYS)

    while True:
        line = [datetime.datetime.now().strftime("%y-%m-%d %H:%M:%S"), redis.read(TS_KEYS[0])[0]]
        for key in TS_KEYS:
            line.append(redis.read(key)[1])
        if int(line[1]/1000) % 20 == 0:
            print(header)
        print(line)
        time.sleep(1)
