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

TODO: Does this require a monitor function?
"""

from serial import SerialException
import logging
import sys
import time
import threading

from thorlabs_apt_device.devices.tdc001 import TDC001
from mkidcontrol.mkidredis import RedisError
import mkidcontrol.mkidredis as redis
import mkidcontrol.util as util
from mkidcontrol.commands import COMMANDSFOCUS, LakeShoreCommand

logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger(__name__)

QUERY_INTERVAL = 1

ENCODER_STEPS_PER_MM = 34555

STATUS_KEY = "status:device:focus:status"

FOCUS_POSITION_MM_KEY = 'status:device:focus:position:mm'
FOCUS_POSITION_ENCODER_KEY = 'status:device:focus:position:encoder'

TS_KEYS = (FOCUS_POSITION_MM_KEY, FOCUS_POSITION_ENCODER_KEY)

MOVE_BY_MM_KEY = 'device-settings:focus:desired-move:mm'
MOVE_BY_ENC_KEY = 'device-settings:focus:desired-move:encoder'

JOG_KEY = 'device-settings:focus:jog'

SETTING_KEYS = tuple(COMMANDSFOCUS.keys())
COMMAND_KEYS = tuple([f"command:{key}" for key in list(SETTING_KEYS) + [MOVE_BY_MM_KEY, MOVE_BY_ENC_KEY, JOG_KEY]])


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
    def position_mm(self):
        return self.status['position']

    @property
    def position_encoder(self):
        return self.status['enc_count']

    @property
    def position(self):
        return {'mm': self.status['position'], 'encoder': self.status['enc_count']}

    def jog(self, direction='forward'):
        """
        Jog the focus stage 'forward' or 'reverse'. This will follow the settings in self.jogparams
        """
        if direction.lower() not in ('forward', 'reverse'):
            raise ValueError(f"Unknown jog direction, '{direction.lower()}'. Usable values are 'forward'/'reverse'")

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

    def move_by(self, dist, units='mm', error_on_disallowed=False):
        """
        Perform a relative move by <dist> <units>
        Position must be provided
        Default units are 'mm', but 'encoder' can also be used for more granular control
        Legal values will depend on the current position.
        If error_on_disallowed = True -> this command will error out without moving the slider
        If error_on_disallowed = False -> the command will warn the user that they are requesting the farthest possible
         move in one direction and then make that move
        Raises a value error if units are in
        """
        if units == 'mm':
            dist_encoder = dist * ENCODER_STEPS_PER_MM
            dist_mm = dist
        elif units == 'encoder':
            dist_mm = dist / ENCODER_STEPS_PER_MM
            dist_encoder = dist
        else:
            raise ValueError(f"Invalid units: '{units}'. Legal values are 'mm' and 'encoder'")

        current_position_enc = self.position_encoder
        desired_position_enc = current_position_enc + dist

        try:
            if desired_position_enc >= self.MAXIMUM_POSITION_ENCODER:
                if error_on_disallowed:
                    log.debug(
                        f"Requested a move by {dist_encoder} ({dist_mm}mm), beyond the maximum allowed position ({self.MAXIMUM_POSITION_ENCODER}/{self.MAXIMUM_POSITION_MM} mm)")
                    raise ValueError(
                        f"Requested a move by {dist_encoder} ({dist_mm}mm), beyond the maximum allowed position ({self.MAXIMUM_POSITION_ENCODER}/{self.MAXIMUM_POSITION_MM} mm)")
                else:
                    log.debug(
                        f"Requested a move by {dist_encoder} ({dist_mm}mm), beyond the maximum allowed position ({self.MAXIMUM_POSITION_ENCODER}/{self.MAXIMUM_POSITION_MM} mm). "
                        f"Moving instead to {self.MAXIMUM_POSITION_ENCODER} ({self.MAXIMUM_POSITION_MM} mm)")
                    dist_encoder = self.MAXIMUM_POSITION_ENCODER - current_position_enc
                    dist_mm = dist_encoder / ENCODER_STEPS_PER_MM
            elif desired_position_enc <= self.MINIMUM_POSITION_ENCODER:
                if error_on_disallowed:
                    log.debug(
                        f"Requested a move by {dist_encoder} ({dist_mm}mm), beyond the minimum allowed position ({self.MINIMUM_POSITION_ENCODER}/{self.MINIMUM_POSITION_MM} mm)")
                    raise ValueError(
                        f"Requested a move by {dist_encoder} ({dist_mm}mm), beyond the minimum allowed position ({self.MINIMUM_POSITION_ENCODER}/{self.MINIMUM_POSITION_MM} mm)")
                else:
                    log.debug(
                        f"Requested a move by {dist_encoder} ({dist_mm}mm), beyond the minimum allowed position ({self.MINIMUM_POSITION_ENCODER}/{self.MINIMUM_POSITION_MM} mm). "
                        f"Moving instead to {self.MINIMUM_POSITION_ENCODER} ({self.MINIMUM_POSITION_MM} mm)")
                    dist_encoder = self.MINIMUM_POSITION_ENCODER - current_position_enc
                    dist_mm = dist_encoder / ENCODER_STEPS_PER_MM
            log.info(f"Attempting to move by {dist_encoder} steps ({dist_mm} mm)")
            self.move_relative(dist_encoder)
            log.info(f"Move successful")
        except (IOError, SerialException) as e:
            raise IOError(f"Error communicating with focus slider: {e}")

    def update_param(self, key, value):
        _, _, param_type, param = key.split(":")
        param.replace('-', '_')
        try:
            to_change = self.params[param_type]
            to_change[param] = value
            if 'home' in param_type:
                self.set_home_params(to_change)
            elif 'jog' in param_type:
                self.set_jog_params(to_change)
            elif 'move' in param_type:
                self.set_move_params(to_change)
            elif 'velocity' in param_type:
                self.set_velocity_params(to_change)
            else:
                raise ValueError(f"Unknown parameter type to update for focus slider!")
        except (IOError, SerialException) as e:
            log.warning(f"Can't communicate with focus slider! {e}")
            raise IOError(f"Can't communicate with focus slider! {e}")

    @property
    def params(self):
        try:
            home_params = self.homeparams_
            jog_params = self.jogparams
            move_params = self.genmoveparams
            vel_params = self.velparams
        except Exception as e:
            raise Exception(f"Error querying params for focus! {e}")

        return {'home': home_params,
                'jog': jog_params,
                'move': move_params,
                'velocity': vel_params}

    def monitor(self, interval: float, monitor_func: (callable, tuple), value_callback: (callable, tuple) = None):
        """
        Given a monitoring function (or is of the same) and either one or the same number of optional callback
        functions call the monitors every interval. If one callback it will get all the values in the order of the
        monitor funcs, if a list of the same number as of monitorables each will get a single value.

        Monitor functions may not return None.

        When there is a 1-1 correspondence the callback is not called in the event of a monitoring error.
        If a single callback is present for multiple monitor functions values that had errors will be sent as None.
        Function must accept as many arguments as monitor functions.
        """
        if not isinstance(monitor_func, (list, tuple)):
            monitor_func = (monitor_func,)
        if value_callback is not None and not isinstance(value_callback, (list, tuple)):
            value_callback = (value_callback,)
        if not (value_callback is None or len(monitor_func) == len(value_callback) or len(value_callback) == 1):
            raise ValueError('When specified, the number of callbacks must be one or the number of monitor functions')

        def f():
            while True:
                vals = []
                for func in monitor_func:
                    try:
                        vals.append(func())
                    except IOError as e:
                        log.error(f"Failed to poll {func}: {e}")
                        vals.append(None)

                if value_callback is not None:
                    if len(value_callback) > 1 or len(monitor_func) == 1:
                        for v, cb in zip(vals, value_callback):
                            try:
                                cb(v)
                            except Exception as e:
                                log.error(f"Callback {cb} error. arg={v}.", exc_info=True)
                    else:
                        cb = value_callback[0]
                        try:
                            cb(*vals)
                        except Exception as e:
                            log.error(f"Callback {cb} error. args={vals}.", exc_info=True)

                time.sleep(interval)

        self._monitor_thread = threading.Thread(target=f, name='Monitor Thread')
        self._monitor_thread.daemon = True
        self._monitor_thread.start()


def callback(pos_mm, pos_enc):
    vals = [pos_mm, pos_enc]
    keys = [FOCUS_POSITION_MM_KEY, FOCUS_POSITION_ENCODER_KEY]
    d = {k: x for k, x in zip(keys, vals)}
    try:
        if all(i is None for i in vals):
            redis.store({STATUS_KEY: "Error"})
        else:
            redis.store(d, timeseries=True)
            redis.store({STATUS_KEY: "OK"})
    except RedisError:
        log.warning('Storing filter wheel data to redis failed!')


if __name__ == "__main__":
    redis.setup_redis(ts_keys=TS_KEYS)
    util.setup_logging('focusAgent')

    try:
        f = Focus(name='focus', port='/dev/focus')
        redis.store({STATUS_KEY: "OK"})
    except RedisError as e:
        log.error(f"Redis server error! {e}")
        sys.exit(1)
    except Exception as e:
        log.critical(f"Could not connect to the filter wheel! Error {e}")
        redis.store({STATUS_KEY: f"Error: {e}"})
        sys.exit(1)

    f.monitor(QUERY_INTERVAL, (f.position_mm, f.position_encoder), value_callback=callback)

    try:
        while True:
            for key, val in redis.listen(COMMAND_KEYS):
                log.debug(f"focusAgent received {key} -> {val}!")
                try:
                    cmd = LakeShoreCommand(key.removeprefix('command:'), val)
                except ValueError as e:
                    log.warning(f"Ignoring invalid command ('{key}={val}'): {e}")
                    continue
                try:
                    if 'params' in key:
                        f.update_param(key, val)
                        redis.store({cmd.setting: cmd.value})
                        redis.store({STATUS_KEY: "OK"})
                    elif 'desired-position' in key:
                        units = key.split(":")[-1]
                        f.move_to(val, units=units)
                    elif key in [MOVE_BY_MM_KEY, MOVE_BY_ENC_KEY]:
                        unts = key.split(":")[-1]
                        f.move_by(val, units=units)
                    elif key == JOG_KEY:
                        f.jog(direction=val)
                except IOError as e:
                    redis.store({STATUS_KEY: f"Error {e}"})
                    log.error(f"Comm error: {e}")
    except RedisError as e:
        log.critical(f"Redis server error! {e}")
        sys.exit(1)

