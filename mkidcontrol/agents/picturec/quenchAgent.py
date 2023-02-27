"""
Author: Noah Swimmer, 3 February 2021

The goal of this program is to monitor for the potential signs of a quench in the PICTURE-C Magnet and, if found,
to report the quench as fast as possible to shut of the magnet and prevent any damage to it.
"""

import mkidcontrol.mkidredis as redis
from mkidcontrol.mkidredis import RedisError
import mkidcontrol.util as util
from mkidcontrol.devices import SIM960
from logging import getLogger
import time
import numpy as np


TS_KEYS = ['status:temps:mkidarray:temp', 'status:highcurrentboard:current',
           'status:temps:lhetank', 'status:temps:ln2tank']
LOOP_INTERVAL = .1
QUENCH_KEY = 'command:event:quenching'

MAX_STARTUP_LAG_TIME_SECONDS = 600

sim = SIM960('/dev/sim960', connect=False)


class QuenchMonitor:
    def __init__(self, npoints:int=30):
        self.npoints_for_smoothing = npoints
        self.timestream = self.initialize_data()
        self.di_dt = self.initialize_di_dt()
        self.smoothed_di_dt = self.initialize_smoothed(self.npoints_for_smoothing)
        self.max_deramp_rate = -1 * sim.MAX_CURRENT_SLOPE


    def update(self):
        new_data = redis.read('status:highcurrentboard:current')
        if new_data[0] == self.timestream[-1][0]:
            pass
        else:
            self.di_dt.append((new_data[0], 1000 * (new_data[1] - self.timestream[-1][1])/(new_data[0] - self.timestream[-1][0])))
            self.timestream.append(new_data)
            ts_for_smooth = np.array(self.timestream[-self.npoints_for_smoothing:])
            self.smoothed_di_dt.append((new_data[0], 1000 * np.polyfit(ts_for_smooth[:, 0], ts_for_smooth[:, 1], 1)[0]))



    def initialize_data(self):
        """
        returns a 2 column array where column 0 is time (ms) and column 1 is current (A) up to the
        last MAX_STARTUP_LAG_TIME_SECONDS. If there is no data in this time range, it will return an empty array
        """
        now = time.time() * 1000
        first_time = MAX_STARTUP_LAG_TIME_SECONDS * 1000
        return redis.pcr_range('status:highcurrentboard:current', int(now-first_time), int(now))

    def initialize_di_dt(self):
        """
        returns a 2 column array where column 0 is time (ms) and column 1 is the current di/dt (A/s). The timestamps
        will match those from initialize_data
        """
        tsl = len(self.timestream)
        di_dt = []
        if tsl > 1:
            for i in range(tsl-1):
                di_dt.append((self.timestream[i+1][0],
                              1000*((self.timestream[i+1][1] - self.timestream[i][1])/
                                    (self.timestream[i+1][0] - self.timestream[i][0]))))
        return di_dt

    def initialize_smoothed(self, npoints:int=30):
        """
        Do smoothing
        """
        tsl = len(self.di_dt)
        ts = np.array(self.timestream)
        smoothed = []
        if tsl > npoints:
            for i in range(tsl-npoints):
                smoothed.append((ts[i+npoints][0],
                                 1000 * np.polyfit(ts[i:i+npoints][:, 0],
                                                   ts[i:i+npoints][:, 1], 1)[0]))
        return smoothed

    def check_quench(self):
        return self.smoothed_di_dt[-1][1] <= 5 * self.max_deramp_rate


if __name__ == "__main__":

    util.setup_logging('quenchAgent')
    redis.setup_redis(ts_keys=TS_KEYS)

    q = QuenchMonitor()

    warning = False
    log = getLogger('quenchAgent')
    log.debug('Starting quench monitoring')

    steps_since_first_quench = 0
    while True:
        try:
            q.update()
            quench = q.check_quench()

            log.debug(f"Checked for quench - quench={quench}")

            if quench:
                steps_since_first_quench += 1
                if warning:
                    redis.publish(QUENCH_KEY, f'QUENCH:{time.time()}', store=False)
                    log.critical(f"Quench detected.")
                else:
                    warning = True
            else:
                if steps_since_first_quench > 0:
                    steps_since_first_quench += 1
                if steps_since_first_quench > 10:
                    warning = False
                    steps_since_first_quench = 0
        except RedisError as e:
            log.critical(f"Redis server error! {e}")
            break
        time.sleep(LOOP_INTERVAL)

