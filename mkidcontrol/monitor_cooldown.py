import numpy as np
import matplotlib.pyplot as plt

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
           'status:temps:device-stage:resistance',
           'status:magnet:current',
           'status:magnet:field']


def plot_cooldown_temperatures(start=None, end=None):
    """
    Create a figure with thermometry data from the 4 temperature stage sensors (50k, 3k, 1k, and Device).
    Takes in start and end timestamps which must be integer UNIX timestamps in milliseconds.
    If start is none, default to 24 hours previous to the query
    If end is none, default to most recent data recorded

    Does not show, plt.show() must be called after this function to see the figure
    """
    redis.setup_redis(ts_keys=TS_KEYS)

    if end is None:
        end = '+'

    if start is None:
        start = int((time.time() - 24 * 60 * 60) * 1000)

    # Query each temperature stage which will be a 2 column array of form [[time1, temp1], [time2, temp2], ...]
    f = np.array(redis.redis_ts.range('status:temps:50k-stage:temp', start, end))
    t = np.array(redis.redis_ts.range('status:temps:3k-stage:temp', start, end))
    o = np.array(redis.redis_ts.range('status:temps:1k-stage:temp', start, end))
    d = np.array(redis.redis_ts.range('status:temps:device-stage:temp', start, end))

    # Create a figure with data from each of the 4 sensors
    plt.figure()
    plt.plot(f[:, 0] / 1000 / 3600 - f[:, 0][0] / 1000 / 3600, f[:, 1], label='50K')
    plt.plot(t[:, 0] / 1000 / 3600 - t[:, 0][0] / 1000 / 3600, t[:, 1], label='3K')
    plt.plot(o[:, 0] / 1000 / 3600 - o[:, 0][0] / 1000 / 3600, o[:, 1], label='1K')
    plt.plot(d[:, 0] / 1000 / 3600 - d[:, 0][0] / 1000 / 3600, d[:, 1], label='Device')
    plt.legend()
    plt.ylabel('Temperature (K)')
    plt.xlabel('Time (Hours)')
    plt.ylim(0, 300)


if __name__ == "__main__":

    header = ['Time', 'Timestamp', '50k T', '50k V', '3k T', '3k V', '1k T', '1k R', 'Device T', 'Device R', 'Magnet Current', 'Magnet Field']

    redis.setup_redis(ts_keys=TS_KEYS)

    while True:
        line = [datetime.datetime.now().strftime("%y-%m-%d %H:%M:%S"), redis.read(TS_KEYS[0])[0]]
        for key in TS_KEYS:
            line.append(redis.read(key)[1])
        if int(line[1]/1000) % 20 == 0:
            print(header)
        print(line)
        time.sleep(1)
