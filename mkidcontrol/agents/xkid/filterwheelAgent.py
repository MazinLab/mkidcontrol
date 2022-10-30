"""
Author: Noah Swimmer, 28 October 2022

Code to control the Finger Lakes Instrumentation (FLI) CFW2-7 Filter wheel
"""

from FLI.filter_wheel import USBFilterWheel


class FilterWheel(USBFilterWheel):
    def __init__(self, name, port=None):
        super.__init__(dev_name=port, model="CFW2-7")

    @property
    def current_filter_position(self):
        """
        Returns the current position of the filter wheel, can be an integer 0 - 6
        """
        return self.get_filter_pos()

    @property
    def filter_count(self):
        """
        Returns the number of filters in the filter wheel.
        For a CFW2-7, it should be 7
        """
        return self.get_filter_count()

