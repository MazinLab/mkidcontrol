"""
A testing script to write generic values to a redis key. Meant for testing plotly.js plotting tools.
"""

from mkidcontrol.pcredis import PCRedis
import numpy as np
import time

if __name__ == "__main__":
    REDIS_DB = 0
    TS_KEYS = ['test_key']

    redis = PCRedis(host='127.0.0.1', port=6379, db=REDIS_DB, create_ts_keys=TS_KEYS)

    vals = np.array([0], dtype=int)
    a = np.arange(0,100)
    vals = np.append(vals, a)
    b = np.flip(np.arange(50,100))
    vals = np.append(vals, b)
    c = np.arange(50,75)
    vals = np.append(vals, c)
    d = np.flip(np.arange(25,75))
    vals = np.append(vals, d)
    e = np.arange(25, 50)
    vals = np.append(vals, e)
    f = np.flip(np.arange(0,50))
    vals = np.append(vals, f)
    vals = np.array(vals, dtype=float)

    for i in vals:
        v = i + np.random.rand()
        print(v, time.time())
        redis.redis_ts.add(key='test_key', value=v, timestamp='*')
        time.sleep(1)
