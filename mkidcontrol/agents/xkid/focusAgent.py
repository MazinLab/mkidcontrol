"""
Author: Noah Swimmer, 26 October 2022

Agent for controlling the Thorlabs TDC001 + MTS50/M-Z8 (50 mm) motor slider which will control the focus position

For the TDC001 source code see the readthedocs page at https://thorlabs-apt-device.readthedocs.io/en/latest/api/thorlabs_apt_device.devices.aptdevice_motor.html
For the MTS50/M-Z8 manual see https://www.thorlabs.com/drawings/b0f5ad357fd27d60-4B9598C7-C024-7FC8-D2B6ACA417A30171/MTS50_M-Z8-Manual.pdf

MTS50/M-Z8 NOTES:
    - The slider has 50 mm of movement.
    - There are 512 encoder counts per revolution of the motor. The motor shaft goes to a 67.49:1 planetary gear head.
    The motor must then rotate 67.49 times to rotate the 1.0 mm pitch screw once (i.e. move the slider by 1.0 mm)
    - There are 512 x 67.49 = 34,555 encoder steps per revolution of the lead screw
    - Each encode count is 1.0 mm / 34,555 encoder steps = 29 nm / encoder step
    - The device can move from 0 - 1727750 (in encoder step space) or 0 - 50 (in mm space)

TODO: All
"""

from serial import SerialException
import time
import logging

from thorlabs_apt_device.devices.tdc001 import TDC001
from mkidcontrol.mkidredis import RedisError
import mkidcontrol.mkidredis as redis
import mkidcontrol.util as util

logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger(__name__)

QUERY_INTERVAL = 1

ENCODER_STEPS_PER_MM = 34555

TS_KEYS = ()


class Focus(TDC001):
    MINIMUM_POSITION_ENCODER = 0
    MINIMUM_POSITION_MM = 0
    MAXIMUM_POSITION_ENCODER = 1727750
    MAXIMUM_POSITION_MM = 50

    def __init__(self, name, port=None, home=False):
        super().__init__(serial_port=port, home=home)
        self.name = name

    def home_slider(self):
        """
        Perform a homing command for the focus slider. Handles serial exceptions
        """
        try:
            self.home()
        except (IOError, SerialException) as e:
            raise IOError(f"Error communicating with focus slider: {e}")

    def stop_slider(self, now=False):
        """
        Stop any motion of the focus slider. Handles serial exceptions
        Can stop <now> by setting now=True or use the default stop command settings with now=False
        """
        try:
            self.stop(immediate=now)
        except (IOError, SerialException) as e:
            raise IOError(f"Error communicating with focus slider: {e}")

    @property
    def position(self):
        return self.status['position']

    def jog(self, direction='forward'):
        """
        Jog the focus stage 'forward' or 'reverse'. This will follow the settings in self.jogparams
        """
        if direction.lower() not in ('forward', 'reverse'):
            raise ValueError(f"Unknown jog direction, ")

        try:
            self.move_jog(direction=direction)
        except (IOError, SerialException) as e:
            raise IOError(f"Error communicating with focus slider: {e}")

    def move_to(self, dest, units='mm', error_on_disallowed=False):
        """
        Perform an absolute move to <position> <units>.
        Position must be provided
        Default units are 'mm', but 'encoder' can also be used for more granular control
        Legal values in mm are [0, 50]
        Legal values in encoder are [0, 1727750]
        """
        if units == 'mm':
            dest_encoder = dest * ENCODER_STEPS_PER_MM
            dest_mm = dest
        elif units == 'encoder':
            dest_mm = dest / ENCODER_STEPS_PER_MM
            dest_encoder = dest
        else:
            raise ValueError(f"Invalid units: '{units}'. Legal values are 'mm' and 'encoder'")

        log.info(f"Move requested to {dest_mm} mm (encoder position {dest_encoder}).")
        try:
            if dest_encoder >= self.MAXIMUM_POSITION_ENCODER:
                if error_on_disallowed:
                    log.debug(f"Requested a move to {dest_encoder} ({dest_mm}mm), beyond the maximum value ({self.MAXIMUM_POSITION_ENCODER}/{self.MAXIMUM_POSITION_MM} mm)")
                    raise ValueError(f"Requested a move to {dest_encoder} ({dest_mm}mm), beyond the maximum value ({self.MAXIMUM_POSITION_ENCODER}/{self.MAXIMUM_POSITION_MM} mm)")
                else:
                    log.debug(f"Requested a move to {dest_encoder} ({dest_mm}mm), beyond the maximum value ({self.MAXIMUM_POSITION_ENCODER}/{self.MAXIMUM_POSITION_MM} mm). "
                              f"Attempting a move to {self.MAXIMUM_POSITION_ENCODER} ({self.MAXIMUM_POSITION_MM} mm)")
                    dest_encoder = self.MAXIMUM_POSITION_ENCODER
                    dest_mm = self.MAXIMUM_POSITION_MM
            elif dest_encoder <= self.MINIMUM_POSITION_ENCODER:
                if error_on_disallowed:
                    log.debug(f"Requested a move to {dest_encoder} ({dest_mm}mm), beyond the minimum value ({self.MINIMUM_POSITION_ENCODER}/{self.MINIMUM_POSITION_MM} mm)")
                    raise ValueError(f"Requested a move to {dest_encoder} ({dest_mm}mm), beyond the minimum value ({self.MINIMUM_POSITION_ENCODER}/{self.MINIMUM_POSITION_MM} mm)")
                else:
                    log.debug(f"Requested a move to {dest_encoder} ({dest_mm}mm), beyond the minimum value ({self.MINIMUM_POSITION_ENCODER}/{self.MINIMUM_POSITION_MM} mm). "
                              f"Moving to {self.MINIMUM_POSITION_ENCODER} ({self.MINIMUM_POSITION_MM} mm)")
                    dest_encoder = self.MINIMUM_POSITION_ENCODER
                    dest_mm = self.MINIMUM_POSITION_MM
            self.move_absolute(dest_encoder)
            log.debug(f"Moved to position {dest_encoder} ({dest_mm} mm)")
        except (IOError, SerialException) as e:
            raise IOError(f"Error communicating with focus slider: {e}")



if __name__ == "__main__":
    redis.setup_redis(ts_keys=TS_KEYS)
    util.setup_logging('focusAgent')

    f = Focus(name='focus', port='/dev/focus')