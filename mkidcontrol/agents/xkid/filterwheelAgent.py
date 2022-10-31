"""
Author: Noah Swimmer, 28 October 2022

Code to control the Finger Lakes Instrumentation (FLI) CFW2-7 Filter wheel

TODO: All
"""

from FLI.filter_wheel import USBFilterWheel
from serial import SerialException

import mkidcontrol.mkidredis as redis
import mkidcontrol.util as util

TS_KEYS = ()

class FilterWheel(USBFilterWheel):
    def __init__(self, name, port=None):
        super.__init__(dev_name=port, model="CFW2-7")
        self.name = name

    @property
    def current_filter_position(self):
        """
        Returns the current position of the filter wheel, can be an integer 0 - 6
        """
        try:
            return self.get_filter_pos()
        except (SerialException, Exception) as e:
            raise Exception(f"Could not communicate with the filter wheel! {e}")

    @property
    def filter_count(self):
        """
        Returns the number of filters in the filter wheel.
        For a CFW2-7, it should be 7
        """
        try:
            return self.get_filter_count()
        except (SerialException, Exception) as e:
            raise Exception(f"Could not communicate with the filter wheel! {e}")

    def move_filter(self, position):
        """
        Sends command to move filter to a new position.
        With the CFW2-7 legal values are 0-6 (for the 7-position wheel)
        """
        try:
            self.set_filter_pos(position)
        except (SerialException, Exception) as e:
            raise Exception(f"Could not communicate with the filter wheel! {e}")


if __name__ == "__main__":

    redis.setup_redis(ts_keys=TS_KEYS)
    util.setup_logging('filterwheelAgent')

    fw = FilterWheel('filterwheel', '/dev/filterwheel')