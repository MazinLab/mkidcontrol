"""
Author: Jeb Bailey & Noah Swimmer

TODO: Add 'off' to devices so they don't just query unnecessarily
"""

from logging import getLogger
import numpy as np
import enum
import logging
import time
import threading
import serial
from serial import SerialException
from lakeshore import InstrumentException
from zaber_motion.binary import Connection, BinarySettings, CommandCode
from FLI.filter_wheel import USBFilterWheel
from thorlabs_apt_device.devices.tdc001 import TDC001

from lakeshore import Model372CurveHeader, Model372CurveFormat, Model336CurveHeader, Model336CurveFormat, \
    Model336CurveTemperatureCoefficients, Model372, Model372CurveTemperatureCoefficient, Model372SensorExcitationMode, \
    Model372MeasurementInputCurrentRange, Model372AutoRangeMode, Model372InputSensorUnits, \
    Model372MeasurementInputResistance, Model372HeaterOutputSettings, Model372OutputMode, Model372InputChannel, \
    Model372InputSetupSettings, Model372ControlInputCurrentRange, Model372MeasurementInputVoltageRange, \
    Model372InputChannelSettings, Model372Polarity, Model372SampleHeaterOutputRange, Model336, \
    Model336InputSensorUnits, Model336InputSensorSettings, Model336InputSensorType, Model336RTDRange, \
    Model336DiodeRange, Model336ThermocoupleRange

from mkidcontrol.mkidredis import RedisError

from mkidcontrol.commands import SimCommand, LakeShoreCommand


log = logging.getLogger(__name__)

Serial = serial.Serial


def escapeString(string):
    """
    Takes a string and escapes newline characters so they can be logged and display the newline characters in that string
    """
    return string.replace('\n', '\\n').replace('\r', '\\r')


def write_persisted_state(statefile, state):
    try:
        with open(statefile, 'w') as f:
            f.write(f'{time.time()}:{state}')
    except IOError:
        log.warning('Unable to log state entry', exc_info=True)
        pass

def load_persisted_state(statefile):
    try:
        with open(statefile, 'r') as f:
            persisted_state_time, persisted_state = f.readline().split(':')
            persisted_state_time, persisted_state = float(persisted_state_time.strip()), persisted_state.strip()
    except Exception:
        persisted_state_time, persisted_state = None, None
    return persisted_state_time, persisted_state


# def firmware_pull(device, redis, firmware_key, model_key, sn_key):
#     # Grab and store device info
#     try:
#         info = device.device_info
#         d = {firmware_key: info['firmware'], model_key: info['model'], sn_key: info['sn']}
#     except IOError as e:
#         log.error(f"When checking device info: {e}")
#         d = {firmware_key: '', model_key: '', sn_key: ''}
#
#     try:
#         redis.store(d)
#     except RedisError:
#         log.warning('Storing device info to redis failed')
#
#
# def initializer(device, setting_keys, redis, firmware_key, model_key, sn_key):
#     """
#     Callback run on connection to the sim whenever it is not initialized. This will only happen if the sim loses all
#     of its settings, which should never every happen. Any settings applied take immediate effect
#     """
#     firmware_pull(device, redis, firmware_key, model_key, sn_key)
#     try:
#         settings_to_load = redis.read(setting_keys, error_missing=True)
#         initialized_settings = device.apply_schema_settings(settings_to_load)
#         time.sleep(1)
#     except RedisError as e:
#         log.critical('Unable to pull settings from redis to initialize LS372')
#         raise IOError(e)
#     except KeyError as e:
#         log.critical('Unable to pull setting {e} from redis to initialize LS372')
#         raise IOError(e)
#
#     try:
#         redis.store(initialized_settings)
#     except RedisError:
#         log.warning('Storing device settings to redis failed')


class MagnetState:
    MANUAL = 0
    PID = 1
    SUM = 2


class HeatswitchPosition:
    OPEN = 'Open'
    CLOSE = 'Close'
    OPENED = 'Opened'
    CLOSED = 'Closed'
    OPENING = 'Opening'
    CLOSING = 'Closing'


class SerialDevice:
    def __init__(self, port, baudrate=115200, timeout=0.1, parity=serial.PARITY_NONE, bytesize=serial.EIGHTBITS,
                 xonxoff=False, stopbits=serial.STOPBITS_ONE, name=None, terminator='\n', response_terminator=''):
        self.ser = None
        self.parity = parity
        self.bytesize = bytesize
        self.port = port
        self.baudrate = baudrate
        self.xonxoff = xonxoff
        self.stopbits = stopbits
        self.timeout = timeout
        self.name = name if name else self.port
        self.terminator = terminator
        self._response_terminator = response_terminator
        self._rlock = threading.RLock()

    def _preconnect(self):
        """
        Override to perform an action immediately prior to connection.
        Function should raise IOError if the serial device should not be opened.
        """
        pass

    def _postconnect(self):
        """
        Override to perform an action immediately after connection. Default is to sleep for twice the timeout
        Function should raise IOError if there are issues with the connection.
        Function will not be called if a connection can not be established or already exists.
        """
        time.sleep(2*self.timeout)

    def _predisconnect(self):
        """
        Override to perform an action immediately prior to disconnection.
        Function should raise IOError if the serial device should not be opened.
        """
        pass

    def connect(self, reconnect=False, raise_errors=True):
        """
        Connect to a serial port. If reconnect is True, closes the port first and then tries to reopen it. First asks
        the port if it is already open. If so, returns nothing and allows the calling function to continue on. If port
        is not already open, first attempts to create a serial.Serial object and establish the connection.
        Raises an IOError if the serial connection is unable to be established.
        """
        if reconnect:
            self.disconnect()

        try:
            if self.ser.isOpen():
                return
        except Exception:
            pass

        log.debug(f"Connecting to {self.port} at {self.baudrate}")
        try:
            self._preconnect()
            self.ser = Serial(port=self.port, baudrate=self.baudrate, timeout=self.timeout,
                              parity=self.parity, bytesize=self.bytesize, xonxoff=self.xonxoff,
                              stopbits=self.stopbits)
            self._postconnect()
            log.getChild('io').info(f"port {self.port} connection established")
            return True
        except (serial.SerialException, IOError) as e:
            self.ser = None
            log.getChild('io').error(f"Conntecting to port {self.port} failed: {e}")
            if raise_errors:
                raise e
            return False

    def disconnect(self):
        """
        First closes the existing serial connection and then sets the ser attribute to None. If an exception occurs in
        closing the port, log the error but do not raise.
        """
        try:
            self._predisconnect()
            self.ser.close()
            self.ser = None
        except Exception as e:
            log.getChild('io').info(f"Exception during disconnect: {e}")

    def format_msg(self, msg:str):
        """Subclass may implement to apply hardware specific formatting"""
        if msg and msg[-1] != self.terminator:
            msg = msg+self.terminator
        return msg.encode('utf-8')

    def send(self, msg: str, connect=True):
        """
        Send a message to a serial port. If connect is True, try to connect to the serial port before sending the
        message. Formats message according to the class's format_msg function before attempting to write to serial port.
        If IOError or SerialException occurs, first disconnect from the serial port, then log and raise the error.
        """
        with self._rlock:
            if connect:
                self.connect()
            try:
                msg = self.format_msg(msg)
                log.getChild('io').debug(f"Sending '{msg}'")
                self.ser.write(msg)
            except (serial.SerialException, IOError) as e:
                self.disconnect()
                log.getChild('io').error(f"...failed: {e}")
                raise e

    def receive(self):
        """
        Receives a message from a serial port. Assumes that the message consists of a single line. If a message is
        received, decode it and strip it of any newline characters. In the case of an error or serialException,
        disconnects from the serial port and raises an IOError.
        """
        with self._rlock:
            try:
                data = self.ser.readline().decode("utf-8")
                log.getChild('io').debug(f"Read {escapeString(data)} from {self.name}")
                if not data.endswith(self._response_terminator):
                    raise IOError("Got incomplete response. Consider increasing timeout.")
                return data.strip()
            except (IOError, serial.SerialException) as e:
                self.disconnect()
                log.getChild('io').debug(f"Send failed {e}")
                raise IOError(e)

    def query(self, cmd: str, **kwargs):
        """
        Send command and wait for a response, kwargs passed to send, raises only IOError
        """
        with self._rlock:
            try:
                self.send(cmd, **kwargs)
                time.sleep(.1)
                return self.receive()
            except Exception as e:
                raise IOError(e)

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
                        log.getChild('io').error(f"Failed to poll {func}: {e}")
                        vals.append(None)

                if value_callback is not None:
                    if len(value_callback) > 1 or len(monitor_func) == 1:
                        for v, cb in zip(vals, value_callback):
                            try:
                                cb(v)
                            except Exception as e:
                                log.getChild('io').error(f"Callback {cb} error. arg={v}.", exc_info=True)
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


class SimDevice(SerialDevice):
    def __init__(self, name, port, baudrate=9600, timeout=0.1, connect=True, initializer=None):
        """The initialize callback is called after _simspecificconnect iff _initialized is false. The callback
        will be passed this object and should raise IOError if the device can not be initialized. If it completes
        without exception (or is not specified) the device will then be considered initialized
        The .initialized_at_last_connect attribute may be checked to see if initilization ran.
        """

        super().__init__(port, baudrate, timeout, name=name, response_terminator='\r\n')

        self.sn = None
        self.firmware = None
        self.mainframe_slot = None
        self.mainframe_exitstring = 'XYZ'
        self.initializer = initializer
        self._monitor_thread = None
        self._initialized = False
        self.initialized_at_last_connect = False
        if connect:
            self.connect(raise_errors=False)

    def _walk_mainframe(self, name):
        """
        Walk the mainframe to find self.name in the device models

        raise KeyError if not present
        raise RuntimeError if not in mainframe mode

        will populate self.firmware and self.sn on success
        """
        id_msg = self.query("*IDN?", connect=False)
        manufacturer, model, _, _ = id_msg.split(",")
        if model != 'SIM900':
            raise RuntimeError('Mainframe not present')

        for slot in range(1, 9):
            self.send(f"CONN {slot}, '{self.mainframe_exitstring}'")
            time.sleep(.1)
            id_msg = self.query("*IDN?", connect=False)
            try:
                manufacturer, model, _, _ = id_msg.split(",")
            except Exception:
                if id_msg == '':
                    log.getChild('io').debug(f"No device in mainframe at slot {slot}")
                    pass
                else:
                    raise IOError(f"Bad response to *IDN?: '{id_msg}'")
            if model == name:
                self.mainframe_slot=slot
                return slot
            else:
                self.send(f"{self.mainframe_exitstring}\n", connect=False)
        raise KeyError(f'{name} not found in any mainframe slot')

    def _predisconnect(self):
        if self.mainframe_slot is not None:
            self.send(f"{self.mainframe_exitstring}\n", connect=False)

    def reset(self):
        """
        Send a reset command to the SIM device. This should not be used in regular operation, but if the device is not
        working it is a useful command to be able to send.
        BE CAREFUL - This will reset certain parameters which are set for us to read out the thermometer in the
        PICTURE-C cryostat (as of 2020, a LakeShore RX102-A).
        If you do perform a reset, it will then be helpful to restore the 'default settings' which we have determined
        to be the optimal to read out the hardware we have.
        """
        log.getChild('io').info(f"Resetting the {self.name}!")
        self.send("*RST")

    def format_msg(self, msg: str):
        return super().format_msg(msg.strip().upper())

    def _simspecificconnect(self):
        pass

    def _preconnect(self):
        time.sleep(1)

    def _postconnect(self):
        try:
            self.send(self.mainframe_exitstring)
            self._walk_mainframe(self.name)
        except RuntimeError:
            pass

        id_msg = self.query("*IDN?", connect=False)
        try:
            manufacturer, model, self.sn, self.firmware = id_msg.split(",")  # See manual page 2-20
        except ValueError:
            log.debug(f"Unable to parse IDN response: '{id_msg}'")
            manufacturer, model, self.sn, self.firmware = [None]*4

        if not (manufacturer == "Stanford_Research_Systems" and model == self.name):
            msg = f"Unsupported device: {manufacturer}/{model} (idn response = '{id_msg}')"
            log.critical(msg)
            raise IOError(msg)

        self._simspecificconnect()

        if self.initializer and not self._initialized:
            self.initializer(self)
            self._initialized = True

    @property
    def device_info(self):
        self.connect(reconnect=False)
        return dict(model=self.name, firmware=self.firmware, sn=self.sn)

    def apply_schema_settings(self, settings_to_load):
        """
        Configure the sim device with a dict of redis settings via SimCommand translation

        In the event of an IO error configuration is aborted and the IOError raised. Partial configuration is possible
        In the even that a setting is not valid it is skipped

        Returns the sim settings and the values per the schema
        """
        ret = {}
        for setting, value in settings_to_load.items():
            try:
                cmd = SimCommand(setting, value)
                log.debug(cmd)
                self.send(cmd.sim_string)
                ret[setting] = value
            except ValueError as e:
                log.warning(f"Skipping bad setting: {e}")
                ret[setting] = self.query(cmd.sim_query_string)
        return ret

    def read_schema_settings(self, settings):
        ret = {}
        for setting in settings:
            cmd = SimCommand(setting)
            ret[setting] = self.query(cmd.sim_query_string)
        return ret


class Focus(TDC001):
    MINIMUM_POSITION_ENCODER = 0
    MINIMUM_POSITION_MM = 0
    MAXIMUM_POSITION_ENCODER = 1727750
    MAXIMUM_POSITION_MM = 50
    ENCODER_STEPS_PER_MM = 34555

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
            dest_encoder = dest * self.ENCODER_STEPS_PER_MM
            dest_mm = dest
        elif units == 'encoder':
            dest_mm = dest / self.ENCODER_STEPS_PER_MM
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
            dist_encoder = dist * self.ENCODER_STEPS_PER_MM
            dist_mm = dist
        elif units == 'encoder':
            dist_mm = dist / self.ENCODER_STEPS_PER_MM
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
                    dist_mm = dist_encoder / self.ENCODER_STEPS_PER_MM
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
                    dist_mm = dist_encoder / self.ENCODER_STEPS_PER_MM
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
            log.getChild('io').warning(f"Can't communicate with focus slider! {e}")
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
                        log.getChild('io').error(f"Failed to poll {func}: {e}")
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


class FilterWheel(USBFilterWheel):
    def __init__(self, name, port=None, model=b"CFW-2-7", filters=None):
        super().__init__(dev_name=port, model=model)
        self.name = name
        self.set_filter_pos(0)  # Initialize to the closed position.
        self.model = model.decode()
        self.filters = filters

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
    def current_filter(self):
        """
        Returns the name of the current filter if a configuration filter dictionary is given
        and self.filters is not None. Otherwise returns None
        """
        if self.filters is None:
            return None
        else:
            try:
                return self.filters[self.current_filter_position]
            except Exception:
                return None

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

    @property
    def serial_number(self):
        """
        Returns the serial number of the FLI filter wheel
        """
        try:
            return self.get_serial_number().decode()
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

    def close(self):
        """
        Convenience function aliasing the self.move_filter function to go to position 'closed'
        """
        if self.filters is not None:
            for k, v in self.filters.items():
                if v.lower() == "closed":
                    closed_pos = k
        else:
            log.warning(f"Unable to determine what filter wheel position corresponds to closed, please provide "
                        f"filterwheelAgent with dict of positions and filters")
            return

        try:
            self.set_filter_pos(closed_pos)
        except (SerialException, Exception) as e:
            raise Exception(f"Could not communicate with the filter wheel! {e}")

    def open(self):
        """
        Convenience function aliasing the self.move_filter function to go to position 'closed'
        """
        if self.filters is not None:
            for k, v in self.filters.items():
                if v.lower() == "open":
                    open_pos = k
        else:
            log.warning(f"Unable to determine what filter wheel position corresponds to open, please provide "
                        f"filterwheelAgent with dict of positions and filters")
            return

        try:
            self.set_filter_pos(open_pos)
        except (SerialException, Exception) as e:
            raise Exception(f"Could not communicate with the filter wheel! {e}")


class HeatswitchMotor:
    TIMEOUT = 4194303 * 1.25 / 0.5e3  # Default timeout value is the number of steps + 25% divided by half the slowest speed we run at
    MOTOR_POS_KEY = "status:device:heatswitch:motor-position"  # Integer between 0 and 4194303
    FULL_CLOSE_POSITION = 4194303  # Halfway point for motor position, physical hard stop with clamps closed on heat sinks
    FULL_OPEN_POSITION = 0  # Hard limit of the motor opening
    DEFAULT_MAX_VELOCITY = 1e3  # Maximum velocity empirically found with ARCONS
    DEFAULT_RUNNING_CURRENT = 13  # Current can be set between 10 (highest) and 127 (lowest). Lower current (higher number)
    # will avoid damaging the heat switch if limit is reached by mistake
    DEFAULT_ACCELERATION = 2  # Default acceleration from ARCONS

    def __init__(self, port, redis_inst, set_mode=True, open_position=None, close_position=None):
        c = Connection.open_serial_port(port)
        self.hs = c.detect_devices()[0]

        self.redis_inst = redis_inst

        self.initialized = False
        self.last_recorded_position = None
        self.last_10_positions = []
        self.last_move = 0

        if open_position:
            self.FULL_OPEN_POSITION = open_position
        if close_position:
            self.FULL_CLOSE_POSITION = close_position

        log.info(f"Heat switch range is ({self.FULL_OPEN_POSITION}, {self.FULL_CLOSE_POSITION}).")

        # Initializes the heatswitch to
        self._initialize_position()

        if set_mode:
            self.update_binary_setting(BinarySettings.DEVICE_MODE, 8)
            self.update_binary_setting(BinarySettings.TARGET_SPEED, self.DEFAULT_MAX_VELOCITY)
            self.update_binary_setting(BinarySettings.RUNNING_CURRENT, self.DEFAULT_RUNNING_CURRENT)
            self.update_binary_setting(BinarySettings.ACCELERATION, self.DEFAULT_ACCELERATION)

        self.running_current = self.hs.settings.get(BinarySettings.RUNNING_CURRENT)
        self.acceleration = self.hs.settings.get(BinarySettings.ACCELERATION)
        self.max_position = min(self.hs.settings.get(BinarySettings.MAXIMUM_POSITION), self.FULL_CLOSE_POSITION)
        self.min_position = self.FULL_OPEN_POSITION
        self.max_velocity = self.hs.settings.get(BinarySettings.TARGET_SPEED)
        self.max_relative_move = self.hs.settings.get(BinarySettings.MAXIMUM_RELATIVE_MOVE)
        self.device_mode = self.hs.settings.get(BinarySettings.DEVICE_MODE)

    def _initialize_position(self):
        """
        :return:
        """
        reported_position = int(self.motor_position())
        last_recorded_position = int(self.redis_inst.read(self.MOTOR_POS_KEY)[1])

        if (reported_position == self.FULL_CLOSE_POSITION) or (last_recorded_position == self.FULL_CLOSE_POSITION):
            self.hs.generic_command(CommandCode.SET_CURRENT_POSITION, last_recorded_position)
            self.redis_inst.store({self.MOTOR_POS_KEY: self.FULL_CLOSE_POSITION}, timeseries=True)

        distance = abs(reported_position - last_recorded_position)

        log.info(f"The last position recorded to redis was {last_recorded_position}. "
                 f"The device thinks it is at a position of {reported_position}, a difference of {distance} steps")

        if distance == 0:
            log.info(f"Device is in the same state as during the previous connection. Motor is in position {last_recorded_position}.")
            # self.hs.generic_command(CommandCode.SET_CURRENT_POSITION, last_recorded_position)
        else:
            log.warning(f"Device was last recorded in position {last_recorded_position}, now thinks that it is at "
                        f"{reported_position}. Setting the position to {last_recorded_position}. If unrecorded movement"
                        f" was made, YOU MUST SET THE CURRENT POSITION MANUALLY")
            self.hs.generic_command(CommandCode.SET_CURRENT_POSITION, last_recorded_position)

        self.initialized = True
        self.last_recorded_position = self.motor_position()

    def state(self):
        if self.motor_position() == self.FULL_CLOSE_POSITION:
            log.debug(f"Motor is {HeatswitchPosition.CLOSED}")
            return HeatswitchPosition.CLOSED
        elif self.motor_position() == self.FULL_OPEN_POSITION:
            log.debug(f"Motor is {HeatswitchPosition.OPENED}")
            return HeatswitchPosition.OPENED
        else:
            if self.last_10_positions[-1] >= self.last_10_positions[-2]:
                log.debug(f"Motor is {HeatswitchPosition.CLOSING}")
                return HeatswitchPosition.CLOSING
            else:
                log.debug(f"Motor is {HeatswitchPosition.OPENING}")
                return HeatswitchPosition.OPENING

    def motor_position(self):
        for i in range(5):
            try:
                position = self.hs.get_position()
                log.debug(f"Motor has reported that it is at position {position}")
                self.last_10_positions.append(position)
                self.last_10_positions = self.last_10_positions[-10:]
                return position
            except (serial.SerialException, Exception) as e:
                log.getChild('io').debug(f"Error in querying heat switch motor. Attempt {i+1} of 5 failed. Trying again.")

    def move_to(self, pos, timeout=TIMEOUT, error_on_disallowed=False):
        """
        TODO: Test and validate
        :param pos:
        :param error_on_disallowed:
        :return:
        """
        last_pos = self.last_recorded_position
        if (last_pos < self.min_position) or (last_pos > self.max_position):
            if last_pos < self.min_position:
                log.warning(f"Requested move to {last_pos} not allowed. Attempting to restrict to FULL_OPEN_POSITION: {self.min_position}")
            elif last_pos > self.max_position:
                log.warning(f"Requested move to {last_pos} not allowed. Attempting to restrict to FULL_CLOSE_POSITION: {self.max_position}")

        if error_on_disallowed:
            raise Exception(f"Move requested from {last_pos} to {pos} not allowed. Out of range")
        else:
            if (last_pos < self.min_position) or (last_pos > self.max_position):
                if last_pos < self.min_position:
                    log.warning(f"Restricting move to FULL_OPEN_POSITION: {self.min_position}. Cannot move to {pos}")
                    pos = self.min_position
                elif last_pos > self.max_position:
                    log.warning(f"Restricting move to FULL_CLOSE_POSITION: {self.max_position}. Cannot move to {pos}")
                    pos = self.max_position
                try:
                    log.info(f"Move requested to {pos} from {last_pos}")
                    self.hs.move_absolute(pos, timeout=timeout)
                    self.last_move = pos - last_pos
                    self.last_recorded_position = pos
                    log.info(f"Successfully moved to {self.last_recorded_position}")
                except:
                    log.error(f"Move failed!!")
            else:
                try:
                    log.info(f"Move requested to {pos} from {last_pos}")
                    self.hs.move_absolute(pos, timeout=timeout)
                    self.last_move = pos - last_pos
                    self.last_recorded_position = pos
                    log.info(f"Successfully moved to {self.last_recorded_position}")
                except:
                    log.error(f"Move failed!!")

        return self.last_recorded_position

    def move_by(self, dist, timeout=TIMEOUT, error_on_disallowed=False):
        """
        :param dist:
        :param error_on_disallowed:
        :return:
        """
        pos = np.copy(self.last_recorded_position)
        if abs(dist) > self.max_relative_move:
            if dist > 0:
                log.warning(f"Requested move of {dist} steps not allowed, restricting to the max value of {self.max_relative_move} steps")
                dist = self.max_relative_move
            elif dist < 0:
                log.warning(f"Requested move of {dist} steps not allowed, restricting to the max value of -{self.max_relative_move} steps")
                dist = -1 * self.max_relative_move

        final_pos = pos + dist
        new_final_pos = min(self.max_position, max(self.min_position, final_pos))

        if new_final_pos != final_pos:
            new_dist = new_final_pos - pos
            if error_on_disallowed:
                raise Exception(f"Move requested from {pos} to {final_pos} ({dist} steps) is not allowed")
            else:
                log.warning(f"Move requested from {pos} to {final_pos} ({dist} steps) is not "
                         f"allowed, restricting move to furthest allowed position of {new_final_pos} ({new_dist} steps).")
                try:
                    new_pos = self.hs.move_relative(new_dist, timeout=timeout)
                    if new_pos == self.motor_position():
                        self.last_recorded_position = new_pos
                        self.last_move = new_dist
                        log.info(f"Successfully moved to {self.last_recorded_position}")
                    else:
                        log.critical(f"Reported motor position ({self.motor_position()}) not equal to expected destination ({new_pos})!\n"
                                     f"Setting last recorded position to {self.motor_position()}")
                        self.last_recorded_position = self.motor_position()
                        self.last_move = self.motor_position() - pos
                except:
                    log.error(f"Move failed!!")
        else:
            log.info(f"Move requested from {pos} to {final_pos} ({dist} steps). Moving now...")
            try:
                new_pos = self.hs.move_relative(dist, timeout=timeout)
                if new_pos == self.motor_position():
                    self.last_recorded_position = new_pos
                    self.last_move = dist
                    log.info(f"Successfully moved to {self.last_recorded_position}")
                else:
                    log.critical(
                        f"Reported motor position ({self.motor_position()}) not equal to expected destination ({new_pos})!\n"
                        f"Setting last recorded position to {self.motor_position()}")
                    self.last_recorded_position = self.motor_position()
                    self.last_move = self.motor_position() - pos
            except:
                log.error(f"Move failed!!")

        return self.last_recorded_position

    def open(self):
        try:
            log.info(f"Opening heatswitch")
            self.move_by(self.FULL_OPEN_POSITION - self.FULL_CLOSE_POSITION)
            log.info(f"Heatswitch now opened")
        except (IOError, serial.SerialException) as e:
            log.getChild('io').error("Could not open heatswitch!")
            raise Exception(f"Could not communicate with device: {e}")
        except Exception as e:
            log.error("Could not open heatswitch!")
            raise Exception(f"Move failed or illegal move requested: {e}")

    def close(self):
        try:
            log.info(f"Closing heatswitch")
            self.move_by(self.FULL_CLOSE_POSITION - self.FULL_OPEN_POSITION)
            log.info(f"Heatswitch now closed")
        except (IOError, serial.SerialException) as e:
            log.getChild('io').error("Could not close the heatswitch!")
            raise Exception(f"Could not communicate with device: {e}")
        except Exception as e:
            log.error("Could not close the heatswitch!")
            raise Exception(f"Move failed or illegal move requested: {e}")

    def update_binary_setting(self, key:(str, BinarySettings), value):
        if isinstance(key, str):
            key = key.split(':')[-1]
            KEYDICT = {'max-velocity': BinarySettings.TARGET_SPEED,
                       'running-current': BinarySettings.RUNNING_CURRENT,
                       'acceleration': BinarySettings.ACCELERATION}
            self.hs.settings.set(KEYDICT[key], value)
        else:
            self.hs.settings.set(key, value)

    def _set_position_value(self, value):
        """
        Tells this heat switch that it is at a different position than it thinks it is
        E.G. If the heatswitch reports that it is at 0 and one uses this function with <value>=10, it will then report
        that it is at position 10 without ever having moved.
        THIS IS SOLELY AN ENGINEERING FUNCTION AND SHOULD ONLY BE USED WITH EXTREME CARE
        """
        self.hs.generic_command(CommandCode.SET_CURRENT_POSITION, value)
        self.move_by(0)

    def stop(self):
        """
        Alias to heatswtich stop
        """
        self.hs.stop()

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
                        log.getChild('io').error(f"Failed to poll {func}: {e}")
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


class LakeShoreDevice(SerialDevice):
    def __init__(self, name, port, baudrate=9600, timeout=0.1, connect=True, valid_models=None,
                 parity=serial.PARITY_ODD, bytesize=serial.SEVENBITS, initializer=None):

        super().__init__(port, baudrate, timeout, name=name, parity=parity, bytesize=bytesize)

        if isinstance(valid_models, tuple):
            self.valid_models = valid_models
        else:
            self.valid_models = tuple(valid_models)

        self.sn = None
        self.firmware = None
        self.terminator = '\n'
        self.initializer = initializer
        self._initialized = False
        self.initialized_at_last_connect = False

        if connect:
            self.connect(raise_errors=False)

    def format_msg(self, msg:str):
        """
        Overrides agent.SerialDevice format_message() function. Commands to the LakeShore are all upper-case.
        *NOTE: By choice, using .upper(), if we manually store a name of a curve/module, it will be in all caps.
        """
        return super().format_msg(msg.strip().upper())

    def _lsspecificconnect(self):
        pass

    @property
    def device_info(self):
        self.connect()
        return dict(model=self.name, firmware=self.firmware, sn=self.sn)

    def _postconnect(self):

        id_msg = self.query("*IDN?")
        try:
            manufacturer, model, self.sn, self.firmware = id_msg.split(",")
        except ValueError:
            log.getChild('io').debug(f"Unable to parse IDN response: '{id_msg}'")
            manufacturer, model, self.sn, self.firmware = [None]*4

        if not (manufacturer == "LSCI") or not (model in self.valid_models):
            msg = f"Unsupported device: {manufacturer}/{model} (idn response = '{id_msg}')"
            log.getChild('io').critical(msg)
            raise IOError(msg)

        if self.name[:-3] == '240':
            self.name += f"-{model[-2:]}"

        self._lsspecificconnect()

        if self.initializer and not self._initialized:
            self.initializer(self)
            self._initialized = True

    def read_schema_settings(self, settings):
        # TODO: Handle "LIMIT?"
        ret = {}
        for setting in settings:
            cmd = LakeShoreCommand(setting)
            ret[setting] = self.query(cmd.ls_query_string)
        return ret

    def apply_schema_settings(self, settings_to_load):
        # TODO: Handle "LIMIT?"
        """
        Configure the sim device with a dict of redis settings via SimCommand translation

        In the event of an IO error configuration is aborted and the IOError raised. Partial configuration is possible
        In the even that a setting is not valid it is skipped

        Returns the sim settings and the values per the schema
        """
        ret = {}
        for setting, value in settings_to_load.items():
            try:
                cmd = LakeShoreCommand(setting, value)
                log.debug(cmd)
                self.send(cmd.ls_string)
                ret[setting] = value
            except ValueError as e:
                log.warning(f"Skipping bad setting: {e}")
                ret[setting] = self.query(cmd.ls_query_string)
        return ret


class LakeShoreMixin:
    """
    Mixin class for functionality that is shared between the MKIDControl wrappers for LakeShore336 and LakeShore372
    devices. Currently, LakeShore has a python package which can be used for communicating with them. We are writing an
    agent for each which uses the Model336/372 as the superclass, while it also uses this Mixin for error handling,
    querying, and parsing of desired setting changes.
    """

    # TODO: Determine protocol for disconnection/connection/reconnection upon erroring out, querying the device, etc.
    def disconnect(self):
        try:
            self.device_serial.close()
        except Exception as e:
            log.getChild('io').info(f"Exception during disconnect: {e}")

    def connect(self):
        try:
            if self.device_serial.isOpen():
                return
        except Exception:
            pass

        try:
            self.device_serial.open()
        except (IOError, AttributeError) as e:
            log.getChild('io').warning(f"Unable to open serial port: {e}")
            raise Exception(f"Unable to open serial port: {e}")

    def _postconnect(self):
        if self.initializer and not self._initialized:
            self.initializer(self)
            self._initialized = True

    @property
    def device_info(self):
        return dict(model=self.model_number, firmware=self.firmware_version, sn=self.serial_number)

    def change_input_sensor_name(self, channel: (str, int), name):
        try:
            log.getChild('io').info(f'Changing name for input sensor at channel {channel.upper()} to "{name}"')
            self.command(f'INNAME {channel.upper}, "{name}"')
        except (SerialException, IOError) as e:
            log.getChild('io').error(f"...failed: {e}")
            raise e

    def get_input_sensor_name(self, channel: (str, int)):
        try:
            log.getChild('io').info(f'Querying name of input sensor at channel {channel.upper()}')
            name = self.query(f'INNAME? {channel.upper}')
            return name.decode('utf-8')
        except (SerialException, IOError) as e:
            log.getChild('io').error(f"...failed: {e}")
            raise e

    def temp(self):
        """
        Returns the temperature for all enabled input channels of the lakeshore temperature controller.
        If there is only 1 channel enabled, returns a float, otherwise returns a list.
        Raises an IOError if there is a problem communicating with the opened serial port
        """
        temp_vals = []
        try:
            for channel in self.enabled_input_channels:
                temp_rdg = float(self.get_kelvin_reading(channel))
                log.info(f"Measured a temperature of {temp_rdg} K from channel {channel}")
                if temp_rdg == 0:
                    log.debug(f"Temperature from channel {channel} was read to be 0. This usually means that temperature"
                              f" is above the calibration limit. Setting to 40K (RX-102A max calibrated temp).")
                    temp_rdg = 40.0
                temp_vals.append(temp_rdg)
        except Exception as e:
            self.disconnect()
            raise IOError(e)

        if len(self.enabled_input_channels) == 1:
            temp_vals = temp_vals[0]

        return temp_vals

    def sensor_vals(self):
        """
        Returns the sensor values for all enabled input channels of the lakeshore temperature controller.
        - For the LakeShore 372, all readings will be resistances
        - For the LakeShore 336, readings can be EITHER resistance or voltage depending on the type of sensor being
          used. The reporting of the proper unit will be handled by the agent itself.
        If there is only 1 channel enabled, returns a float, otherwise returns a list.
        Raises an IOError if there is a problem communicating with the opened serial port
        """
        readings = []
        try:
            for channel in self.enabled_input_channels:
                if self.model_number == "MODEL372":
                    res = float(self.get_resistance_reading(channel))
                    log.info(f"Measured a resistance of {res} Ohms from channel {channel}")
                    readings.append(res)
                elif self.model_number == "MODEL336":
                    sens = float(self.get_sensor_reading(channel))
                    log.info(f"Measured a value of {sens} from channel {channel}")
                    readings.append(sens)
        except Exception as e:
            self.disconnect()
            raise IOError(e)

        if len(self.enabled_input_channels) == 1:
            readings = readings[0]

        return readings

    def excitation_power(self):
        """
        Returns the excitation power for all enabled input channels of the lakeshore 372. Not implemented in the
        lakeshore 336.
        If there is only 1 channel enabled, returns a float, otherwise returns a list.
        Raises an IOError if there is a problem communicating with the opened serial port
        """
        readings = []
        try:
            for channel in self.enabled_input_channels:
                pwr = float(self.get_excitation_power(channel))
                log.info(f"Measured an excitation power of {pwr} W from channel {channel}")
                readings.append(pwr)
        except Exception as e:
            self.disconnect()
            raise IOError(e)

        if len(self.enabled_input_channels) == 1:
            readings = readings[0]

        return readings

    def query_single_setting(self, schema_key, command_code):
        _, inst, c, key = schema_key.split(":")
        key = key.replace("-", "_")
        c = c.split("-")

        if c[-2] == "channel":
            channel = c[-1]
            curve = None
        elif c[-2] == "curve":
            curve = c[-1]
            channel = None

        try:
            settings = self.query_settings(command_code, channel, curve)
        except Exception as e:
            raise IOError(e)

        try:
            return settings[key]
        except (AttributeError, TypeError):
            return settings

    def query_settings(self, command_code, channel=None, curve=None):
        """
        Using a command code (from either the COMMANDS336 or COMMANDS372 dict) and either a channel or curve number,
        sends the appropriate query to the lakeshore device. If the result that gets returned is a class instance,
        parses it using the vars() function to turn it into a dict where each key is the property name and each value is
        its corresponding value.
        This is used by the 'modification' functions to query the current configuration of a channel or curve, which can
        then be modified and have any subset of those settings changed (if allowable).
        Raises an IOError in case of a serial hiccup.

        TODO: Consider pulling from redis as opposed to querying the device itself
        """
        model = self.model_number

        if channel is None and curve is None:
            raise ValueError(f"Insufficient information to query a channel or a curve!")

        try:
            if command_code == "INTYPE":
                if model == "MODEL336":
                    data = vars(self.get_input_sensor(str(channel)))
                elif model == "MODEL372":
                    data = vars(self.get_input_setup_parameters(str(channel)))
                log.getChild('io').debug(f"Read input sensor data for channel {channel}: {data}")
            elif command_code == "INCRV":
                data = self.get_input_curve(channel)
                log.getChild('io').debug(f"Read input curve number for channel {channel}: {data}")
            elif command_code == "INSET":
                data = vars(self.get_input_channel_parameters(channel))
                log.getChild('io').debug(f"Reading parameters for input channel {channel}: {data}")
            elif command_code == "OUTMODE":
                data = vars(self.get_heater_output_settings(channel))
                log.getChild('io').debug(f"Read heater settings for heater channel {channel}: {data}")
            elif command_code == "SETP":
                data = self.get_setpoint_kelvin(channel)
                log.getChild('io').debug(f"Read setpoint for heater channel {channel}: {data} Kelvin")
            elif command_code == "PID":
                data = self.get_heater_pid(channel)
                log.getChild('io').debug(f"Read PID settings for channel {channel}: {data}")
            elif command_code == "RANGE":
                data = self.get_heater_output_range(channel)
                log.getChild('io').debug(f"Read the current heater output range for channel {channel}: {data}")
            elif command_code == "CRVHDR":
                data = vars(self.get_curve_header(curve))
                log.getChild('io').debug(f"Read the curve header from curve {curve}: {data}")
            elif command_code == "FILTER":
                data = vars(self.get_filter(channel))
                log.getChild('io').debug(f"Read the filter data for channel {channel}: {data}")
            return data
        except (IOError, SerialException) as e:
            raise IOError(f"Serial error communicating with Lake Shore {self.model_number[-3:]}: {e}")
        except ValueError as e:
            log.critical(f"{channel} is not an allowed channel for the Lake Shore {self.model_number[-3:]}: {e}."
                         f"Ignoring request")

    def _generate_new_settings(self, channel=None, curve=None, command_code=None, **desired_settings):
        """
        Uses the command code (string from the 'COMMAND' key in the LAKESHORE_COMMANDS dict) along with a curve/channel
        number to first query the current settings for whatever setting is desired to be changed.
        Next, takes the dictionary that is returned by the query_settings() function and iterates through the
        **desired_settings. The new_settings dictionary will be populated with the same keys as returned by the query_settings
        call. If any of the keys are present as keys in the **desired_settings, those will be added as the values in the
        new_settings dict, otherwise they will remain the same as in the query. The new_settings dict is then returned
        to be used by one of the 'modify_...' functions.
        """
        if command_code is None:
            raise IOError(f"Insufficient information to query {self.model_num[-3:]}, no command code given.")

        try:
            if channel is not None:
                settings = self.query_settings(command_code, channel=channel)
            elif curve is not None:
                settings = self.query_settings(command_code, curve=curve)
            else:
                log.error(f"Insufficient values given for curve or channel to query! Cannot generate up-to-date settings."
                          f"Ignoring request to modify settings.")
                raise IOError(f"Insufficient value given to query channel/curve")
        except (SerialException, IOError) as e:
            raise e

        new_settings = {}
        for k in settings.keys():
            try:
                new_settings[k] = desired_settings[k]
            except KeyError:
                new_settings[k] = settings[k]

        return new_settings

    def modify_curve_header(self, curve_num, command_code, **desired_settings):
        """
        Follows the standard modify_<setting>() pattern. Updates a user-specifiable curve header. This command will not
        work if the user attempts to modify a preset curve on either the LakeShore 336 or 372. Primarily useful for
        when a user is loading in a new curve and wants to name it, set its serial number, etc. (see kwargs in the
        function below).
        Will raise an IOError in the case of a serial error
        """
        new_settings = self._generate_new_settings(curve=curve_num, command_code=command_code, **desired_settings)

        if self.model_number == "MODEL372":
            header = Model372CurveHeader(curve_name=new_settings['curve_name'],
                                         serial_number=new_settings['serial_number'],
                                         curve_data_format=Model372CurveFormat(new_settings['curve_data_format']),
                                         temperature_limit=new_settings['temperature_limit'],
                                         coefficient=Model372CurveTemperatureCoefficient(new_settings['coefficient']))
        elif self.model_number == "MODEL336":
            header = Model336CurveHeader(curve_name=new_settings['curve_name'],
                                         serial_number=new_settings['serial_number'],
                                         curve_data_format=Model336CurveFormat(new_settings['curve_data_format']),
                                         temperature_limit=new_settings['temperature_limit'],
                                         coefficient=Model336CurveTemperatureCoefficients(new_settings['coefficient']))
        else:
            raise ValueError(f"Attempting to modify an curve to an unsupported device!")

        try:
            log.getChild('io').info(f"Applying new curve header to curve {curve_num}: {header}")
            self.set_curve_header(curve_number=curve_num, curve_header=header)
        except (SerialException, IOError) as e:
            log.getChild('io').error(f"...failed: {e}")
            raise IOError(f"{e}")

    def load_curve_data(self, curve_num, data=None, data_file=None):
        """
        Curve_num is the desired curve to load data into. Valid options are 21-59.

        If data_file is not none, loads the data from the given .txt file on the system (there is not current support
        for files of other formats such as .npz) . The expected format is 2 columns, column 0 is the sensor values and
        column 1 is the associated calibrated temperature values. The temeprature values should always run high to low.
        If data is not none, it is understood that the user is passing the data directly to the function. The format for
        the data should be the same as in the description for the data_file.
        data_file will take priority over data.

        # TODO: Format checking
        """
        if data:
            curve_data = data
        elif data_file:
            curve_data = np.loadtxt(data_file)
        else:
            raise ValueError(f"No data supplied to load to the curve")

        self.set_curve(curve_num, curve_data)

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
                        log.getChild('io').error(f"Failed to poll {func}: {e}")
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


class LakeShore240(LakeShoreDevice):
    def __init__(self, name, port, baudrate=115200, timeout=0.1, connect=True, valid_models=None, parity=serial.PARITY_NONE, bytesize=serial.EIGHTBITS):
        super().__init__(name, port, baudrate, timeout, connect=connect, valid_models=valid_models, parity=parity, bytesize=bytesize)

        self._monitor_thread = None  # Maybe not even necessary since this only queries
        self.last_he_temp = None
        self.last_ln2_temp = None

    def _postconnect(self):
        super()._postconnect()
        enabled = []
        for channel in range(1, int(self.name[-2]) + 1):
            try:
                _, _, enabled_status = self.query(f"INTYPE? {channel}").rpartition(',')
                if enabled_status == "1":
                    enabled.append(channel)
            except IOError as e:
                log.getChild('io').error(f"Serial error: {e}")
                raise IOError(f"Serial error: {e}")
            except ValueError:
                log.critical(f"Channel {channel} returned and unknown value from channel information query")
                raise IOError(f"Channel {channel} returned and unknown value from channel information query")
        self.enabled = tuple(enabled)

    def read_temperatures(self):
        """Queries the temperature of all enabled channels on the LakeShore 240. LakeShore reports values of temperature
        in Kelvin. May raise IOError in the case of serial communication not working."""
        readings = []
        tanks = ['ln2', 'lhe']
        for channel in self.enabled:
            try:
                readings.append(float(self.query(f"KRDG? {channel}")))
            except IOError as e:
                log.getChild('io').error(f"Serial Error: {e}")
                raise IOError(f"Serial Error: {e}")
            except ValueError as e:
                log.error(f"Parsing error: {e}")
                raise ValueError(f"Parsing error: {e}")
        temps = {tanks[i]: readings[i] for i in range(len(self.enabled))}
        return temps

    def _set_curve_name(self, channel: int, name: str):
        """Engineering function to set the name of a curve on the LakeShore240. Convenient since both thermometers are
        DT-670A-CU style, and so this can clear any ambiguity. Does not need to be used in normal operation. Logs
        IOError but does not raise it.
        """
        try:
            self.send(f'INNAME{str(channel)},"{name}"')
        except IOError as e:
            log.getChild('io').error(f"Unable to set channel {channel}'s name to '{name}'. "
                      f"Check to make sure the LakeShore USB is connected!")


class LakeShore336(LakeShoreMixin, Model336):
    def __init__(self, name, port=None, timeout=0.1, enabled_channels=(), initializer=None):
        """
        Initialize the LakeShore336 unit. Requires a name, typically something like 'LakeShore336' or '336'.
        The port and timeout parameters are optional. If port is none, the __init__() function from the Model 336 super
        class will search the device tree for units which have the correct PID/VID combination. If timeout is none, it
        will default to 0.1 seconds, which is lower than the default of 2 seconds in the superclass.
        """
        self.device_serial = None
        self.enabled_input_channels = enabled_channels
        self.initializer = initializer
        self._initialized = False

        if port is None:
            super().__init__(timeout=timeout)
        else:
            super().__init__(com_port=port, timeout=timeout)
        self.name = name
        self._postconnect()

    def change_curve(self, channel, command_code, curve_num=None):
        """
        Takes in an input channel and the relevant command code from the LAKESHORE_COMMANDS dict to query what the
        current calibration curve is in use. If the curve_num given is not none or the same as the one which is already
        loaded in, it will attempt to change to a new calibration curve for that input channel
        If no curve number is given or the user tries to change to the current curve (i.e. Channel A uses Curve 2, try
        switching to curve 2), no change will be made.
        """
        current_curve = self.query_settings(command_code, channel=channel)

        if current_curve != curve_num and curve_num is not None:
            try:
                log.getChild('io').info(f"Changing curve for input channel {channel} from {current_curve} to {curve_num}")
                self.set_input_curve(channel, curve_num)
            except (SerialException, IOError) as e:
                log.getChild('io').error(f"...failed: {e}")
                raise e

        else:
            log.warning(f"Requested to set channel {channel}'s curve from {current_curve} to {curve_num}, no change"
                     f"sent to Lake Shore {self.model_number}.")

    def modify_input_sensor(self, channel: (str, int), command_code, **desired_settings):
        """
        Reads in the current settings of the input sensor at channel <channel>, changes any setting passed as an
        argument that is not 'None', and stores the modified dict of settings in dict(new_settings). Then reads the
        new_settings dict into a Model336InputSettings object and sends the appropriate command to update the input
        settings for that channel
        """
        new_settings = self._generate_new_settings(channel=channel, command_code=command_code, **desired_settings)

        if new_settings['sensor_type'] == 0:
            new_settings['input_range'] = None
        elif new_settings['sensor_type'] == 1:
            new_settings['input_range'] = Model336DiodeRange(new_settings['input_range'])
        elif new_settings['sensor_type'] in (2, 3):
            new_settings['input_range'] = Model336RTDRange(new_settings['input_range'])
        elif new_settings['sensor_type'] == 4:
            new_settings['input_range'] = Model336ThermocoupleRange(new_settings['input_range'])
        else:
            raise ValueError(f"{new_settings['sensor_type']} is not an allowed value!")

        settings = Model336InputSensorSettings(sensor_type=Model336InputSensorType(new_settings['sensor_type']),
                                               autorange_enable=new_settings['autorange_enable'],
                                               compensation=new_settings['compensation'],
                                               units=Model336InputSensorUnits(new_settings['units']),
                                               input_range=new_settings['input_range'])

        try:
            log.getChild('io').info(f"Applying new settings to channel {channel}: {settings}")
            self.set_input_sensor(channel=channel, sensor_parameters=settings)
        except (SerialException, IOError) as e:
            log.getChild('io').error(f"...failed: {e}")
            raise e

    def apply_schema_settings(self, settings_to_load):
        """
        Configure the sim device with a dict of redis settings via SimCommand translation

        In the event of an IO error configuration is aborted and the IOError raised. Partial configuration is possible
        In the even that a setting is not valid it is skipped

        Returns the sim settings and the values per the schema
        """
        ret = {}
        for setting, value in settings_to_load.items():
            try:
                cmd = LakeShoreCommand(setting, value)
                log.debug(f"Setting LakeShore 336 {cmd.setting} to {cmd.value}")
                self.handle_command(cmd)
                ret[setting] = value
            except ValueError as e:
                log.warning(f"Skipping bad setting: {e}")
                ret[setting] = self.query_single_setting(cmd.setting, cmd.command_code)
            time.sleep(0.2)
        return ret

    def handle_command(self, cmd):
        try:
            log.info(f"Processing command {cmd.setting} -> {cmd.value}")
            if cmd.command_code == "INTYPE":
                self.modify_input_sensor(channel=cmd.channel, command_code=cmd.command_code, **cmd.desired_setting)
            elif cmd.command_code == "INCRV":
                self.change_curve(channel=cmd.channel, command_code=cmd.command_code, curve_num=cmd.command_value)
            elif cmd.command_code == "CRVHDR":
                self.modify_curve_header(curve_num=cmd.curve, command_code=cmd.command_code, **cmd.desired_setting)
            elif cmd.command_code == "INNAME":
                self.change_input_sensor_name(channel=cmd.channel, name=cmd.command_value)
            else:
                pass
        except IOError as e:
            log.getChild('io').error(f"Comm error: {e}")
            raise e


class LakeShore372(LakeShoreMixin, Model372):
    def __init__(self, name, baudrate=57600, port=None, timeout=0.1, enabled_input_channels=(), initializer=None):

        self.device_serial = None
        self.enabled_input_channels = enabled_input_channels
        self.initializer = initializer
        self._initialized = False

        if port is None:
            super().__init__(baud_rate=baudrate, timeout=timeout)
        else:
            super().__init__(baud_rate=baudrate, com_port=port, timeout=timeout)
        self.name = name
        self._postconnect()

    def apply_schema_settings(self, settings_to_load):
        """
        Configure the sim device with a dict of redis settings via SimCommand translation

        In the event of an IO error configuration is aborted and the IOError raised. Partial configuration is possible
        In the even that a setting is not valid it is skipped

        Returns the sim settings and the values per the schema
        """
        ret = {}
        for setting, value in settings_to_load.items():
            try:
                cmd = LakeShoreCommand(setting, value)
                log.debug(f"Setting LakeShore 372 {cmd.setting} to {cmd.value}")
                self.handle_command(cmd)
                ret[setting] = value
            except ValueError as e:
                log.warning(f"Skipping bad setting: {e}")
                ret[setting] = self.query_single_setting(cmd.setting, cmd.command_code)
            time.sleep(0.2)
        return ret

    def handle_command(self, cmd):
        try:
            log.info(f"Processing command {cmd.setting} -> {cmd.value}")
            if cmd.command_code == "INTYPE":
                self.configure_input_sensor(channel=cmd.channel, command_code=cmd.command_code,
                                                 **cmd.desired_setting)
            elif cmd.command_code == "INSET":
                self.modify_channel_settings(channel=cmd.channel, command_code=cmd.command_code,
                                                  **cmd.desired_setting)
            elif cmd.command_code == "OUTMODE":
                self.configure_heater_settings(channel=cmd.channel, command_code=cmd.command_code,
                                                    **cmd.desired_setting)
            elif cmd.command_code == "SETP":
                self.change_temperature_setpoint(channel=cmd.channel, command_code=cmd.command_code,
                                                      setpoint=cmd.command_value)
            elif cmd.command_code == "PID":
                self.modify_pid_settings(channel=cmd.channel, command_code=cmd.command_code, **cmd.desired_setting)
            elif cmd.command_code == "RANGE":
                self.modify_heater_output_range(channel=cmd.channel, command_code=cmd.command_code,
                                                     range=cmd.command_value)
            elif cmd.command_code == "CRVHDR":
                self.modify_curve_header(curve_num=cmd.curve, command_code=cmd.command_code, **cmd.desired_setting)
            elif cmd.command_code == "INNAME":
                self.change_input_sensor_name(channel=cmd.channel, name=cmd.command_value)
            elif cmd.command_code == "FILTER":
                self.set_channel_filter(channel=cmd.channel, command_code=cmd.command_code, **cmd.desired_setting)
            else:
                log.info(f"Command code '{cmd.command_code}' not recognized! No change will be made")
                pass
        except Exception as e:
            self.disconnect()
            log.getChild('io').error(f"Comm error: {e}")
            raise IOError(e)

    @property
    def setpoint(self):
        """
        Returns the setpoint for the sample heater in Kelvin
        """
        try:
            return self.get_setpoint_kelvin(0)
        except Exception as e:
            self.disconnect()
            raise IOError(e)

    def output_voltage(self):
        """
        Returns the current output to the sample heater in percent of total output
        Range runs from 0 to 100%
        """
        try:
            return self.get_heater_output(0)
        except Exception as e:
            self.disconnect()
            raise IOError(e)

    def configure_input_sensor(self, channel, command_code, **desired_settings):
        """
        Takes in an allowable channel number, command code (to query the current settings), and the desired settings to
        modify in order to configure the input sensor for the given channel.
        """
        new_settings = self._generate_new_settings(channel=channel, command_code=command_code, **desired_settings)

        if channel.upper() == "A":
            new_settings['excitation_range'] = Model372ControlInputCurrentRange(new_settings['excitation_range'])
        else:
            if new_settings['mode'] == 0:
                new_settings['excitation_range'] = Model372MeasurementInputVoltageRange(new_settings['excitation_range'])
            elif new_settings['mode'] == 1:
                new_settings['excitation_range'] = Model372MeasurementInputCurrentRange(new_settings['excitation_range'])
            else:
                raise ValueError(f"{new_settings['mode']} is not an allowed value!")

        settings = Model372InputSetupSettings(mode=Model372SensorExcitationMode(new_settings['mode']),
                                              excitation_range=new_settings['excitation_range'],
                                              auto_range=Model372AutoRangeMode(new_settings['auto_range']),
                                              current_source_shunted=new_settings['current_source_shunted'],
                                              units=Model372InputSensorUnits(new_settings['units']),
                                              resistance_range=Model372MeasurementInputResistance(new_settings['resistance_range']))

        try:
            log.getChild('io').info(f"Configuring input sensor on channel {channel}: {settings}")
            self.configure_input(input_channel=channel, settings=settings)
        except (SerialException, IOError) as e:
            log.getChild('io').error(f"...failed: {e}")
            raise e

    def modify_channel_settings(self, channel, command_code, **desired_settings):
        """
        Takes in an allowable channel number, command code (to query the current settings), and the desired settings to
        modify in order to modify the settings of how the channel reads out the sensor and how it is reported.
        This is the command for the LakeShore 372 where the calibration curve can be changed
        """
        new_settings = self._generate_new_settings(channel=channel, command_code=command_code, **desired_settings)

        settings = Model372InputChannelSettings(enable=new_settings['enable'],
                                                dwell_time=new_settings['dwell_time'],
                                                pause_time=new_settings['pause_time'],
                                                curve_number=new_settings['curve_number'],
                                                temperature_coefficient=Model372CurveTemperatureCoefficient(new_settings['temperature_coefficient']))

        try:
            log.getChild('io').info(f"Configuring input channel {channel} parameters: {settings}")
            self.set_input_channel_parameters(channel, settings)
        except (SerialException, IOError) as e:
            log.getChild('io').error(f"...failed: {e}")
            raise e

    def configure_heater_settings(self, channel, command_code, **desired_settings):
        """
        Takes in an allowable channel number, command code (to query the current settings), and the desired settings to
        modify in order to configure the settings for the output heater from the LakeShore 372.
        """
        new_settings = self._generate_new_settings(channel=channel, command_code=command_code, **desired_settings)

        settings = Model372HeaterOutputSettings(output_mode=Model372OutputMode(new_settings['output_mode']),
                                                input_channel=Model372InputChannel(new_settings['input_channel']),
                                                powerup_enable=new_settings['powerup_enable'],
                                                reading_filter=new_settings['reading_filter'],
                                                delay=new_settings['delay'],
                                                polarity=Model372Polarity(new_settings['polarity']))

        try:
            log.getChild('io').info(f"Configuring heater for output channel {channel}: {settings}")
            self.configure_heater(output_channel=channel, settings=settings)
        except (SerialException, IOError) as e:
            log.getChild('io').error(f"...failed: {e}")
            raise e

    def change_temperature_setpoint(self, channel, command_code, setpoint=None):
        """
        Takes in an allowable channel number, command code (to query the current settings), and the new setpoint the
        user would like to control the device at. Setpointwill always be in units of Kelvin.
        """
        current_setpoint = self.query_settings(command_code, channel=channel)
        if current_setpoint != setpoint and setpoint is not None:
            log.info(f"Changing temperature regulation value for output channel {channel} to {setpoint} from "
                     f"{current_setpoint}")
            try:
                log.getChild('io').info(f"Changing the setpoint for output channel {channel} to {setpoint}")
                self.set_setpoint_kelvin(output_channel=channel, setpoint=setpoint)
            except (SerialException, IOError) as e:
                log.getChild('io').error(f"...failed: {e}")
                raise e
        else:
            log.info(f"Requested to set temperature setpoint from {current_setpoint} to {setpoint}, no change"
                        f"sent to Lake Shore 372.")

    def set_channel_filter(self, channel, command_code, **desired_settings):
        """
        Takes in an allowable channel number, command code (to query the current settings), and the desired settings to
        modify in order to set a filter on an input channel
        """
        new_settings = self._generate_new_settings(channel=channel, command_code=command_code, **desired_settings)
        try:
            log.getChild('io').info(f"Configuring filter for input channel {channel}: {new_settings}")
            self.set_filter(channel, state=new_settings['state'], settle_time=new_settings['settle_time'],
                            window=new_settings['window'])
        except (SerialException, IOError) as e:
            log.getChild('io').error(f"...failed: {e}")
            raise e

    def modify_pid_settings(self, channel, command_code, **desired_settings):
        """
        Takes in an allowable channel number, command code (to query the current settings), and the desired settings to
        modify in order to update the PID loop. Desired settings can be 'gain', 'integral', or 'derivative', for the
        P, I, and D parameters, respectively (a value of 0 means the term is unused).
        """
        new_settings = self._generate_new_settings(channel=channel, command_code=command_code, **desired_settings)

        try:
            log.getChild('io').info(f"Configuring PID for output channel {channel}: {new_settings}")
            self.set_heater_pid(channel, gain=new_settings['gain'], integral=new_settings['integral'],
                                derivative=new_settings['ramp_rate'])
        except (SerialException, IOError) as e:
            log.getChild('io').error(f"...failed: {e}")
            raise e

    def modify_heater_output_range(self, channel, command_code, range=None):
        """
        Takes in an allowable channel number, command code (to query the current settings), and the desired heater range
        from the allowed values, which step from 31.6 uA to 100 mA stepping up by a factor of 3 each step.
        """
        current_range = self.query_settings(command_code, channel=channel)

        if channel == 0:
            if current_range.value == range or range is None:
                log.info(f"Attempting to set the output range for the output heater from {current_range.name} to the "
                         f"same value. No change requested to the instrument.")
            else:
                try:
                    log.getChild('io').info(f"Setting the output range of channel {channel} from {current_range} to {range}")
                    self.set_heater_output_range(channel, Model372SampleHeaterOutputRange(range))
                except (SerialException, IOError) as e:
                    log.getChild('io').error(f"...failed: {e}")
                    raise e
        else:
            # For a channel that is not the sample heater, this value must be on or off
            if current_range == range or range is None:
                log.info(f"Attempting to set the output range for the output heater from {current_range} to the "
                         f"same value. No change requested to the instrument.")
            else:
                try:
                    log.getChild('io').info(f"Setting the output range of channel {channel} from {current_range} to {range}")
                    self.set_heater_output_range(channel, range)
                except (SerialException, IOError) as e:
                    log.getChild('io').error(f"...failed: {e}")
                    raise e


class LakeShore625(LakeShoreDevice):
    MAX_CURRENT = 9.4

    def __init__(self, port, baudrate=9600, parity=serial.PARITY_ODD, bytesize=serial.SEVENBITS, timeout=0.1, connect=True, valid_models=None, initializer=None):

        self.last_current_read = None
        self.last_field_read = None
        self.last_voltage_read = None

        self.current_limit = None  # The max current one can send to the LS625
        self.voltage_limit = None  # The greatest (magnitude) voltage the LS625 can apply across the magnet
        self.rate_limit = None  # The greatest (magnitude) current ramp rate the LS625 will allow the user to set
        self.limits_cached = False

        super().__init__("LS625", port, baudrate=baudrate, timeout=timeout, parity=parity, bytesize=bytesize,
                         connect=connect, valid_models=valid_models, initializer=initializer)

        self.send(f"XPGM 2")  # Make sure the Lake Shore 625 is always in summing mode
        # self.send("SETI 0.000") N.B. If a restart happens with current in the magnet, we want it to stay that way and
        #                                 not cause quenches

    @property
    def limits(self):
        if self.limits_cached:
            log.debug(f"Limits have been cached, not querying device")
        else:
            try:
                log.getChild('io').info(f"Querying limits from Lake Shore 625")
                current_limit, voltage_limit, rate_limit = self.query("LIMIT?").split(',')
            except (IOError, SerialException) as e:
                log.getChild('io').warning(f"Could not query the limits from the Lake Shore 625!")
                raise IOError(f"Can't communicate with the Lake Shore 625: {e}")
            self.current_limit = float(current_limit)
            self.voltage_limit = float(voltage_limit)
            self.rate_limit = float(rate_limit)
            self.limits_cached = True
        return {'current': self.current_limit, 'voltage': self.voltage_limit, 'rate': self.rate_limit}

    def _lsspecificconnect(self):
        # mode = self.query("XPGM?")
        # current = self.query("SETI?")
        #
        # if int(mode) == 0 and float(current) == 0.0:
        #     self._initialized = True
        # else:
        #     self._initialized = False
        # self.initialized_at_last_connect = self._initialized
        pass

    def current(self):
        current = self.query("RDGI?")
        self.last_current_read = current
        return current

    def set_desired_current(self, current):
        self.send(f"SETI {current}")

    def field(self):
        field = self.query("RDGF?")
        self.last_field_read = field
        return field

    def output_voltage(self):
        voltage = self.query("RDGV?")
        self.last_voltage_read = voltage
        return voltage

    @property
    def mode(self):
        """ Returns MagnetState or raises ValueError (which means we don't know!) """
        try:
            mode = self.query(f"XPGM?")
        except (IOError, SerialException) as e:
            log.getChild('io').warning(f"Can't communicate with the LS 625: {e}")
            raise IOError(f"Can't communicate with the LS 625: {e}")
        if mode == '0':
            return MagnetState.MANUAL
        elif mode == '1':
            return MagnetState.PID
        elif mode == '2':
            return MagnetState.SUM
        else:
            raise ValueError(f"Invalid external program mode: '{mode}'")

    @mode.setter
    def mode(self, value: int):
        """ Set the magnet state, state may not be set of Off directly.
        If transistioning to manual ensure that the manual current doesn't hiccup
        """
        with self._rlock:
            mode = int(self.mode)
            log.debug(f"Setting Lake Shore 625 mode from {mode} to {value}")
            if mode == value:
                return
            if value == MagnetState.SUM:
                self.send("XPGM 2")
                self.zero_current()
            elif value == MagnetState.MANUAL:
                self.send("XPGM 0")
                self.zero_current()
            elif value == MagnetState.PID:
                self.send("XPGM 1")
            else:
                log.warning(f"Mode {mode} is invalid for the Lake Shore 625. Allowed values are 0, 1, 2")

    def kill_current(self):
        """
        Commands the lakeshore 625 to have a very high ramp rate, then sets the current to 0A
        """
        self.send("RATE 10")  # Let the current drop very quickly
        self.send("SETI 0.000")  # Set current immediately to 0
        self.send("RATE 0.005")  # Set current change rate (dI/dt) back to the default value of 0.005 A/s

    def zero_current(self):
        """
        Sends a command to the lakeshore 625 to set its current back to 0A
        """
        self.send("SETI 0.000")

    def stop_ramp(self):
        self.send("STOP")


class SIM960(SimDevice):

    MAX_CURRENT_SLOPE = .015  # 15 mA/s
    MAX_CURRENT = 10.0
    OFF_SLOPE = 0.5

    def __init__(self, port, baudrate=9600, timeout=0.1, connect=True, initializer=None):
        """
        Initializes SIM960 agent. First hits the superclass (SerialDevice) init function. Then sets class variables which
        will be used in normal operation. If connect mainframe is True, attempts to connect to the SIM960 via the SIM900
        in mainframe mode. Raise IOError if an invalid slot or exit string is given (or if no exit string is given).
        """
        self.polarity = 'negative'
        self.last_input_voltage = None
        self.last_output_voltage = None
        self._last_manual_change = time.time() - 1  # This requires that in the case the program fails that systemd does
        # not try to restart the sim960Agent program more frequently than once per second (i.e. if sim960Agent crashes,
        # hold off on trying to start it again for at least 1s)
        super().__init__('SIM960', port, baudrate, timeout, connect=connect, initializer=initializer)

    @property
    def state(self):
        """
        Return offline, online, or configured

        NB configured implies that settings have not been lost due to a power cycle
        """
        try:
            polarity = self.query("APOL?", connect=True)
            return 'configured' if int(polarity)==0 else 'online'
        except IOError:
            return 'offline'

    def _simspecificconnect(self):
        polarity = self.query("APOL?", connect=False)
        if int(polarity) == 1:
            self.send("APOL 0", connect=False)  # Set polarity to negative, fundamental to the wiring.
            polarity = self.query("APOL?", connect=False)
            if polarity != '0':
                msg = f"Polarity query returned {polarity}. Setting PID loop polarity to negative failed."
                log.critical(msg)
                raise IOError(msg)
            self._initialized = False
            self.initialized_at_last_connect = False
        else:
            self._initialized = polarity == '0'
            self.initialized_at_last_connect = self._initialized

    def input_voltage(self):
        """Read the voltage being sent to the input monitor of the SIM960 from the SIM921"""
        iv = float(self.query("MMON?"))
        self.last_input_voltage = iv
        return iv

    def output_voltage(self):
        """Report the voltage at the output of the SIM960. In manual mode, this will be explicitly controlled using MOUT
        and in PID mode this will be the value set by the function Output = P(e + I * int(e) + D * derv(e)) + Offset"""
        ov = float(self.query("OMON?"))
        self.last_output_voltage = ov
        return ov

    @staticmethod
    def _out_volt_2_current(volt:float, inverse=False):
        """
        Converts a sim960 output voltage to the expected current.
        :param volt:
        :param inverse:
        If true -> enter a current, return the voltage needed for it
        If false -> enter a voltage, return the current it will produce
        :return:
        """
        if inverse:
            return (volt - 0.00869474) / 1.30007052
        else:
            return 1.30007052 * volt + 0.00869474

    def setpoint(self):
        """ return the current that is currently commanded by the sim960 """
        return self._out_volt_2_current(self.output_voltage())

    @property
    def manual_current(self):
        """
        return the manual current setpoint. Queries the manual output voltage and converts that to the expected current.
        'MOUT?' query returns the value of the user-specified output voltage. This will only be the output voltage in manual mode (not PID).

        0.004 volts are added to the manual_voltage_setpoint because the output_voltage (OMON?) is always ~4mV greater
        than the desired value (MOUT). Since it is not EXACTLY 4mV, setpoint and manual_current may return slightly
        different values.
        """
        manual_voltage_setpoint = float(self.query("MOUT?")) + 0.002
        return self._out_volt_2_current(manual_voltage_setpoint)

    @manual_current.setter
    def manual_current(self, x: float):
        """
        will clip to the range 0,MAX_CURRENT and enforces a maximum absolute current derivative

        0.004 is subtracted from the desired voltage to be commanded because when setting MOUT, the actual output
        voltage (OMON) is always ~4mV greater than specified (MOUT)
        """
        if not self._initialized:
            raise ValueError('Sim is not initialized')
        x = min(max(x, 0), self.MAX_CURRENT)
        delta = abs((self.setpoint() - x)/(time.time()-self._last_manual_change))
        if delta > self.MAX_CURRENT_SLOPE:
            raise ValueError('Requested current delta unsafe')
        self.mode = MagnetState.MANUAL
        self.send(f'MOUT {self._out_volt_2_current(x, inverse=True) - 0.002:.4f}')  # Response, there's mV accuracy, so at least 3 decimal places
        self._last_manual_change = time.time()

    def kill_current(self):
        """Immediately kill the current"""
        self.mode=MagnetState.MANUAL
        self.send(f'MOUT {self._out_volt_2_current(0, inverse=True) - 0.002:.4f}')


    @property
    def mode(self):
        """ Returns MagnetState or raises IOError (which means we don't know!) """
        return MagnetState.MANUAL if self.query('AMAN?') == '0' else MagnetState.PID

    @mode.setter
    def mode(self, value: MagnetState):
        """ Set the magnet state, state may not be set of Off directly.
        If transistioning to manual ensure that the manual current doesn't hiccup
        """
        with self._rlock:
            mode = self.mode
            if mode == value:
                return
            if value == MagnetState.MANUAL:
                self.send(f'MOUT {self._out_volt_2_current(self.setpoint(), inverse=True):.3f}')
                self.send("AMAN 0")
                #NB no need to set the _lat_manual_change time as we arent actually changing the current
            else:
                self.send("AMAN 1")


class SIM921OutputMode:
    SCALED = 'scaled'
    MANUAL = 'manual'


class SIM921(SimDevice):
    def __init__(self, port, timeout=0.1, connect=True, initializer=None):
        super().__init__(name='SIM921', port=port, baudrate=9600, timeout=timeout, connect=connect,
                         initializer=initializer)
        self.scale_units = 'resistance'
        self.last_voltage = None
        self.last_monitored_values = None
        self._monitor_thread = None
        self.last_voltage_read = None
        self.last_temp_read = None
        self.last_resistance_read = None

    def _simspecificconnect(self):
        # Ensure that the scaled output will be proportional to the resistance error. NOT the temperature error. The
        # resistance spans just over 1 order of magnitude (~1-64 kOhms) while temperature spans 4 (5e-2 - 4e2 K).
        self.send("ATEM 0", connect=False)
        atem = self.query("ATEM?", connect=False)
        if atem != '0':
            msg = (f"Setting ATEM=0 failed, got '{atem}'. Zero, indicating voltage scale units are in resistance, "
                   "is required. DO NOT OPERATE! Exiting.")
            log.critical(msg)
            raise IOError(msg)

        # Make sure that the excitation is turned on. If not successful we can't use the device
        self.send("EXON 1", connect=False)
        exon = self.query("EXON?", connect=False)
        if exon != '1':
            msg = f"EXON=1 failed, got '{exon}'. Unable to enable excitation and unable to operate!"
            log.critical(msg)
            raise IOError(msg)

    def temp(self):
        temp = self.query("TVAL?")
        self.last_temp_read = temp
        return temp

    def resistance(self):
        res = self.query("RVAL?")
        self.last_resistance_read = res
        return res

    def output_voltage(self):
        aman = self.query("AMAN?")
        if aman == "1":
            log.debug("SIM921 voltage output is in manual mode!")
            voltage = self.query("AOUT?")
        elif aman == "0":
            log.debug("SIM921 voltage output is in scaled mode!")
            voltage = float(self.query("VOHM?")) * float(self.query("RDEV?"))
        else:
            msg = f"SIM921 did not respond to AMAN? with 0 or 1! -> '{aman}'"
            log.critical(msg)
            raise IOError(msg)
        self.last_voltage_read = voltage
        return voltage

    def temp_and_resistance(self):
        return {'temperature': self.temp, 'resistance': self.resistance}

    def convert_temperature_to_resistance(self, temperature:float, curve:int):
        if curve not in (1, 2, 3):
            log.error(f"SIM921 does not have a valid curve loaded. "
                      f"There is no calibrated matching resistance value to {temperature}")
            return 0

        if curve == 1:
            import pkg_resources as pkg
            file = pkg.resource_filename('hardware.thermometry.RX-102A', 'RX-102A_Mean_Curve.tbl')
        else:
            log.error(f"Curve {curve} has not been implemented yet. No matching resistance value to {temperature}")
            return 0

        try:
            curve_data = np.loadtxt(file)
            temp_data = np.flip(curve_data[:, 0], axis=0)
            res_data = np.flip(curve_data[:, 1], axis=0)
        except OSError:
            log.error(f"Could not find curve data file.")
            raise ValueError(f"{file} couldn't be loaded.")
        except IndexError:
            raise ValueError(f"{file} couldn't be loaded.")

        if temperature in temp_data:
            log.info(f"{temperature} K is a regulatable temperature.")
            m = temperature == temp_data
            return float(res_data[m])
        else:
            log.warning(f"{temperature} K is not a regulatable temperature.")
            return 0

    def _load_calibration_curve(self, curve_num: int, curve_type, curve_name: str, file:str=None):
        """
        This is an engineering function for the SIM921 device. In normal operation of the fridge, the user should never
        have to load a curve in. This should only ever be used if (1) a new curve becomes available, (2) the
        thermometer used by the SIM921 is changed out for a new one, or (3) the original curve becomes corrupted.
        Currently (21 July 2020) designed specifically to read in the LakeShore RX-102-A calibration curve, but can be
        modified without difficulty to take in other curves. The command syntax will not change for loading the curve
        onto the SIM921, only the np.loadtxt() and data manipulation of the curve data itself. As long as the curve
        is in a format where resistance[n] < resistance[n+1] for all points n on the curve, it can be loaded into the
        SIM921 instrument.
        """
        if curve_num not in (1, 2, 3):
            log.error(f"SIM921 only accepts 1, 2, or 3 as the curve number")
            return None

        CURVE_TYPE_DICT = {'linear': '0', 'semilogt': '1', 'semilogr': '2', 'loglog': '3'}
        if curve_type not in CURVE_TYPE_DICT.keys():
            log.error(f"Invalid calibration curve type for SIM921. Valid types are {CURVE_TYPE_DICT.keys()}")
            return None

        if file is None:
            import pkg_resources as pkg
            file = pkg.resource_filename('hardware.thermometry.RX-102A', 'RX-102A_Mean_Curve.tbl')

        log.info(f"Curve data at {file}")

        try:
            curve_data = np.loadtxt(file)
            temp_data = np.flip(curve_data[:, 0], axis=0)
            res_data = np.flip(curve_data[:, 1], axis=0)
        except OSError:
            log.error(f"Could not find curve data file.")
            raise ValueError(f"{file} couldn't be loaded.")
        except IndexError:
            raise ValueError(f"{file} couldn't be loaded.")

        log.info(f"Attempting to initialize curve {curve_num}, type {curve_type}")
        try:
            self.send(f"CINI {curve_num}, {CURVE_TYPE_DICT[curve_type]}, {curve_name}")
            for t, r in zip(temp_data, res_data):
                self.send(f"CAPT {curve_num}, {r}, {t}")
                time.sleep(0.1)
        except IOError as e:
            raise e
        log.info(f"Successfully loaded curve {curve_num} - '{curve_name}'!")


class Currentduino(SerialDevice):
    VALID_FIRMWARES = (0.0, 0.1, 0.2)
    R1 = 11760  # Values for R1 resistor in magnet current measuring voltage divider
    R2 = 11710  # Values for R2 resistor in magnet current measuring voltage divider

    def __init__(self, port, baudrate=115200, timeout=0.1, connect=True):
        super().__init__(port, baudrate, timeout, name='currentduino')
        if connect:
            self.connect(raise_errors=False)
        self.heat_switch_position = None
        self._monitor_thread = None
        self.last_current = None
        self.terminator = ''

    def read_current(self):
        """
        Read and return the current, may raise ValueError (unparseable response) or IOError (serial port communcation
        not working for some reason)"""
        response = self.query('?', connect=True)
        try:
            value = float(response.split(' ')[0])
            voltage = (value * (5.0 / 1023.0) * ((self.R1 + self.R2) / self.R2))
            if value > 0:
                current = ((2.84324895 * voltage) + 0.0681135)
            else:
                current = 0
        except ValueError:
            raise ValueError(f"Could not parse '{response}' into a float")
        log.info(f"Current value is {current} A")
        return current

    def _postconnect(self):
        """
        Overwrites SerialDevice _postconnect function. The default 2 * timeout is not sufficient to let the arduino to
        set up, so a slightly longer pause is implemented here
        """
        time.sleep(2)

    def format_msg(self, msg: str):
        """
        Overwrites function from SerialDevice superclass. Follows the communication model we made where the arduinos in
        PICTURE-C do not require termination characters.
        """
        return f"{msg.strip().lower()}{self.terminator}".encode()

    def move_heat_switch(self, pos):
        """
        Takes a position (open | close) and first checks to make sure that it is valid. If it is, send the command to
        the currentduino to move the heat switch to that position. Return position if successful, otherwise log that
        the command failed and the heat switch position is 'unknown'. Raise IOError if there is a problem communicating
        with the serial port.
        """
        pos = pos.lower()
        if pos not in (HeatswitchPosition.OPEN, HeatswitchPosition.CLOSE):
            raise ValueError(f"'{pos} is not a vaild ({HeatswitchPosition.OPEN}, {HeatswitchPosition.CLOSE})'"
                             f"' heat switch position")

        try:
            log.info(f"Commanding heat switch to {pos}")
            confirm = self.query(pos[0], connect=True)
            if confirm == pos[0]:
                log.getChild('io').info(f"Command accepted")
            else:
                log.getChild('io').info(f"Command failed: '{confirm}'")
            return pos if confirm == pos[0] else 'unknown'
        except Exception as e:
            raise IOError(e)

    def firmware_ok(self):
        """ Return True or False if the firmware is supported, may raise IOErrors """
        return self.firmware in self.VALID_FIRMWARES

    def check_hs_pos(self, pos):
        """ Return True if the HS is in the expected position, False if it is not. """
        return True
        # if pos not in (HeatswitchPosition.OPEN, HeatswitchPosition.CLOSE):
        #     raise ValueError(f"'{pos} is not a vaild ({HeatswitchPosition.OPEN}, {HeatswitchPosition.CLOSE})'"
        #                      f"' heat switch position")
        #
        # # NB: The same sensor on the HS checks for open/closed HS position. If there is a thermal touch (HS close) it
        # #  will report a LOW voltage (GND) and if there are no touches (HS open) it will report a HIGH voltage (+5V)
        # try:
        #     log.getChild('io').debug(f"Checking Heatswitch position is {pos}")
        #     response = self.query('h' if pos[0] == 'o' else 'l')
        #     pos, _, desired = response.partition(" ")
        #     return pos == desired
        # except IOError as e:
        #     log.getChild('io').error(f"Serial error: {e}")
        #     raise e

    @property
    def firmware(self):
        """ Return the firmware string or raise IOError """
        try:
            log.getChild('io').debug(f"Querying currentduino firmware")
            response = self.query("v", connect=True)
            version, _, v = response.partition(" ")  # Arduino resonse format is "{response} {query char}"
            version = float(version)
            if v != "v":
                raise ValueError('Bad format')
            return version
        except IOError as e:
            log.getChild('io').error(f"Serial error: {e}")
            raise e
        except ValueError:
            log.getChild('io').error(f"Bad firmware format: '{response}'")
            raise IOError(f'Bad firmware response: "{response}"')

    def monitor_current(self, interval, value_callback=None):
        """
        Create a function to continuously query the current as measured by the arduino. Log any IOErrors that occur.
        If a value_callback is given (e.g. for storing values to redis), call it and pass over any exceptions it
        generates. Interval determines the time between queries of current.
        """
        def f():
            while True:
                current = None
                try:
                    self.last_current = self.read_current()
                    current = self.last_current
                except (IOError, ValueError) as e:
                    log.getChild('io').error(f"Unable to poll for current: {e}")

                if value_callback is not None and current is not None:
                    try:
                        value_callback(self.last_current)
                    except Exception as e:
                        log.error(f"Exception during value callback: {e}")
                        pass

                time.sleep(interval)

        self._monitor_thread = threading.Thread(target=f, name='Current Monitoring Thread')
        self._monitor_thread.daemon = True
        self._monitor_thread.start()


class Hemtduino(SerialDevice):
    VALID_FIRMWARES = (0.0, 0.1)

    def __init__(self, port, baudrate=115200, timeout=0.1, connect=True):
        super().__init__(port, baudrate, timeout, name='hemtduino')
        if connect:
            self.connect(raise_errors=False)
        self.terminator = ''

    def _postconnect(self):
        """
        Overwrites serialDevice _postconnect function. Sleeps for an appropriate amount of time to let the arduino get
        booted up properly so the first queries don't return nonsense (or nothing)
        """
        time.sleep(1)

    def format_msg(self, msg:str):
        """
        Overwrites the format_msg function from SerialDevice. Returns a lowercase string with the hemtduino terminator
        (which is '' in the contract with the hemtduino).
        """
        return f"{msg.strip().lower()}{self.terminator}".encode("utf-8")

    def firmware_ok(self):
        """
        Return True if the reported firmware is in the list of valid firmwares for the hemtduino.
        """
        return self.firmware in self.VALID_FIRMWARES

    @property
    def firmware(self):
        """ Return the firmware string or raise IOError """
        try:
            log.getChild('io').debug(f"Querying currentduino firmware")
            response = self.query("v", connect=True)
            version, _, v = response.partition(" ")  # Arduino resonse format is "{response} {query char}"
            version = float(version)
            if v != "v":
                raise ValueError('Bad format')
            return version
        except IOError as e:
            log.getChild('io').error(f"Serial error: {e}")
            raise e
        except ValueError:
            log.getChild('io').error(f"Bad firmware format: '{response}'")
            raise IOError(f'Bad firmware response: "{response}"')

    def read_hemt_data(self):
        """
        Return the hemt data in the order received from the hemtduino (it reads A0 -> A14).
        This reports the bias voltages read. It does not convert to current for the gate current values. Raises a value
        error if a bad response is returned (the arduino does not report back the query string as the final character)
        or a nonsense string is returned that is unparseable.
        """
        response = self.query('?', connect=True)
        try:
            resp = response.split(' ')
            values = list(map(float, resp[:-1]))
            confirm = resp[-1]
            if confirm == '?':
                log.getChild('io').debug("HEMT values successfully queried")
                pvals = []
                for i, voltage in enumerate(values):
                    if not i % 3:
                        pvals.append(2 * ((voltage * (5.0 / 1023.0)) - 2.5))
                    if not (i+1) % 3:
                        pvals.append(voltage * (5.0 / 1023.0))
                    if not (i+2) % 3:
                        pvals.append(voltage * (5.0 / 1023.0) / 0.1)

                pvals = [v * (5.0 / 1023.0) if i % 3 else 2 * ((v * (5.0 / 1023.0)) - 2.5) for i, v in enumerate(values)]
                return pvals
            else:
                raise ValueError(f"Nonsense was returned: {response}")
        except Exception as e:
            raise ValueError(f"Error parsing response data: {response}. Exception {e}")

class Laserflipperduino(SerialDevice):
    VALID_FIRMWARES = (0.0, 0.1)

    def __init__(self, port, baudrate=115200, timeout=1, connect=True, lasernames=None):
        super().__init__(port, baudrate, timeout, name='laserflipperduino')
        if connect:
            self.connect(raise_errors=True)
        self.status = {0: 0.0,
                       1: 0.0,
                       2: 0.0,
                       3: 0.0,
                       4: 0.0,
                       5: 0.0}
        self.terminator = ''
        self.names = lasernames

    def _postconnect(self):
        """
        Overwrites serialDevice _postconnect function. Sleeps for an appropriate amount of time to let the arduino get
        booted up properly so the first queries don't return nonsense (or nothing)
        """
        time.sleep(2)

    def format_msg(self, msg):
        """
        Overwrites function from SerialDevice superclass.
        msg is expected to be either a tuple, array, or bytearray of length 2
        """
        return bytearray(msg)

    def send(self, msg: (bytearray, tuple), connect=True):
        """
        Send a message to a serial port. If connect is True, try to connect to the serial port before sending the
        message. Formats message according to the class's format_msg function before attempting to write to serial port.
        If IOError or SerialException occurs, first disconnect from the serial port, then log and raise the error.
        """
        with self._rlock:
            if connect:
                self.connect()
            try:
                msg = self.format_msg(msg)
                log.getChild('io').debug(f"Sending '{msg}'")
                self.ser.write(msg)
            except (serial.SerialException, IOError) as e:
                self.disconnect()
                log.getChild('io').error(f"...failed: {e}")
                raise e

    @property
    def firmware(self):
        """ Return the firmware string or raise IOError """
        try:
            log.getChild('io').debug(f"Querying currentduino firmware")
            response = self.query((7, 0), connect=True)
            _, version = response.split(':')
            return float(version)
        except IOError as e:
            log.getChild('io').error(f"Serial error: {e}")
            raise e
        except ValueError:
            log.getChild('io').error(f"Bad firmware format: '{response}'")
            raise IOError(f'Bad firmware response: "{response}"')

    def firmware_ok(self):
        """ Return True or False if the firmware is supported, may raise IOErrors """
        return self.firmware in self.VALID_FIRMWARES

    def set_diode(self, index, value):
        """Set diode sets the pwm of a laser diode, the inputs are the diode
        index and a value of what fraction of current to apply

        index is the index of the diode going from 0 to 4 with the mapping
            defined by the names global list
        value is a value from 0 to 100 setting how much current to apply with 1
            being the max current defined by the resistors on the board
        """
        if (value < 0) or (value > 100):
            raise ValueError('invalid power setting')
        elif not isinstance(index, int) or (index < 0) or (index > 4):
            raise ValueError('invalid laser index')
        else:
            pwm_byte = int(value / 100 * 255)
            message = (index, pwm_byte)
            pin, val = self.query(message).split(':')
            val = int(val) / 255 * 100
            self.status[int(pin)] = val  # Convert from 0-255 bit value to percentage
            log.info(f"Pin {index} ({self.names[index]} laser) set to {val:.2f}%")

    def set_mirror_position(self, position):
        """sett_mirror_position takes a position argument to move the mirror
        flipper ot the right position

        position should be a numerical value, 0 moved the flipper down and a
            non-zero value sets it to the up position
        """
        if position.lower() == 'down':
            log.debug(f"Setting mirror to down")
            byte_val = 0
        elif position.lower() == 'up':
            log.debug(f"Setting mirror to up")
            byte_val = 1
        else:
            raise ValueError(f"Illegal mirror position requested: '{position}'. Legal values are ('down', 'up')")
        pin, val = self.query((5, byte_val)).split(':')
        self.status[int(pin)] = int(val)
        if val == 0:
            log.info(f"Mirror flipped down")
        else:
            log.info(f"Mirror flipped up")

    def statuses(self):
        """get_status takes no arguments, prints the status of all 5 output
        pins"""
        log.getChild('io').debug("Reading laser and mirror statuses")
        statuses = {}
        status_reply = self.query((6,0))
        status_reply = status_reply.split(',')
        for laser in status_reply:
            dat = laser.split(':')
            amp_value = float(dat[1]) / 255 * 100  # Convert to a percentage
            self.status[int(dat[0])] = amp_value
            statuses[self.names[int(dat[0])]] = amp_value
        return statuses


class Conex(SerialDevice):
    CONTROLLER_STATES = {"14": "CONFIGURATION",
                         "28": "MOVING CL",
                         "29": "STEPPING OL",
                         "32": "READY from Reset",
                         "33": "READY from MOVING CL",
                         "34": "READY from DISABLE",
                         "35": "READY from JOGGING OL",
                         "36": "READY from STEPPING OL",
                         "3C": "DISABLE from READY OL",
                         "3D": "DISABLE from MOVING CL",
                         "46": "JOGGING OL"}

    def __init__(self, port, controller=1, timeout=1, connect=True, initializer=None):
        super().__init__(name="Conex", port=port, baudrate=921600, timeout=timeout, bytesize=serial.EIGHTBITS,
                         stopbits=serial.STOPBITS_ONE, xonxoff=True, terminator='\r\n')
        pass

        self.ctrlN = controller  # Controller can be an int between 1-31 inclusive or a string of 1 to 2 characters that
        # represents the possible number values (i.e. 1, "1", and "01" will all work)

        self.u_lower_limit = -np.inf
        self.v_lower_limit = -np.inf
        self.u_upper_limit = np.inf
        self.v_upper_limit = np.inf

        self.initializer = initializer
        self._monitor_thread = None
        self._initialized = False
        self.initialized_at_last_connect = False

        if connect:
            self.connect(raise_errors=False)
            q = [float(self.query(q)) for q in ('SLU?', 'SLV?', 'SRU?', 'SRV?')]
            self.u_lower_limit = q[0]
            self.v_lower_limit = q[1]
            self.u_upper_limit = q[2]
            self.v_upper_limit = q[3]

    @property
    def id_number(self):
        return self.query("ID?")

    @property
    def firmware(self):
        return self.query("VE?")

    @property
    def limits(self):
        """
        Hardware limit for U, V in degrees
        returns dict with keys (umin, umax, vmin, vmax)
        """
        return dict(umin=self.u_lower_limit, vmin=self.v_lower_limit,
                    umax=self.u_upper_limit, vmax=self.v_upper_limit)

    def set_limit(self, cmd:str, limit:(str, float)):
        msg = cmd+str(limit)
        try:
            log.debug(f"Setting {cmd} with value {limit}")
            self.send(msg)
        except Exception as e:
            raise IOError(f"Failed to command conex: {e}")
        try:
            new_limit = float(self.query(f"{cmd}?"))
        except Exception as e:
            raise IOError(f"Failed to query new limit value: {e}")

        if cmd == "SLU":
            self.u_lower_limit = new_limit
        elif cmd == "SLV":
            self.v_lower_limit = new_limit
        elif cmd == "SRU":
            self.u_upper_limit = new_limit
        elif cmd == "SRV":
            self.v_upper_limit = new_limit
        else:
            raise ValueError(f"Invalid limit command sent! {cmd}")

    def status(self):
        """
        Check status of the conex

        :return: Tuple of (status code, status string, status message)
        :raises: IOError if there are communication issues
        """
        status_msg = self.query("TS?")
        err = status_msg[:4]
        status_code = status_msg[4:]

        if err == '0020':
            raise IOError("Motion time out")
        elif int(err, 16) > 0:
            raise IOError(f"Unknown Err - {err}")

        try:
            status = self.CONTROLLER_STATES[status_code]
        except (KeyError, ValueError) as e:
            raise ValueError(f"Invalid status code read by conex: {e}")

        return (status_code, status, status_msg)

    def ready(self):
        """
        Check status of the conex

        :return
        True if conex is ready for another command
        False is conex isn't ready
        :raises
        IOError if there are communication issues
        """
        # TODO: Just call self.status[0] to get the status_code?
        # status = self.status()
        # return int(status[0]) in (32, 33, 34, 35, 36)

        status_msg = self.query("TS?")
        err = status_msg[:4]
        status = status_msg[4:]

        if err == '0020':
            raise IOError("Motion time out")
        elif int(err, 16) > 0:
            raise IOError(f"Unknown Err - {err}")

        return int(status) in (32, 33, 34, 35, 36)

    def move(self, pos:(tuple, list, np.array), blocking=False, timeout=5.):
        """
        Move mirror to new position

        :param pos: [U,V] tuple/list/array position in degrees (Conex truncates these floats at 3 decimal places)
        :param blocking: If True, don't return until the move is complete
        :param timeout: error out if it takes too long for move to complete. Ignored if not blocking. Requires
         significant time even though the moves themselves are fast
        """
        with self._rlock:
            if not self.in_bounds(position=pos):
                raise ValueError('Target position outside of limits. Aborted move')
            self.send(f"PAU{pos[0]}")
            self.ser.flush()  # wait until the write command finishes sending
            self.send(f"PAV{pos[1]}")  # Conex can move both axes at once
            if blocking:
                self.ser.flush()
        if blocking:
            t = time.time()
            while not self.ready():
                if time.time() - t > timeout:
                    status = self.status()
                    raise IOError(f"Move timed out. Status: {status[1]} (code {status[0]})")
                time.sleep(0.001)

    def home(self, blocking=False):
        """
        Move the conex back to position (0, 0)
        """
        self.move((0, 0), blocking=blocking)

    def position(self):
        u_pos = self.query("TPU?")
        v_pos = self.query("TPV?")
        return (float(u_pos), float(v_pos))

    def in_bounds(self, position:(tuple, list, np.array)=None, u:float=None, v:float=None):
        """
        :param Either position in the format [u,v] or u AND v
        Position must be type <float> in degrees
        The position tuple (u,v) will supersede individual coordinates being passed
        :
        :return: True if position is within the positioning limits, False otherwise
        """
        if position is None:
            if (u is None) or (v is None):
                raise ValueError(f"Cannot determine position is in bounds without coordinates (either [u,v] or u and v)")
        else:
            u = position[0]
            v = position[1]

        inbounds = ((self.u_lower_limit <= u <= self.u_upper_limit) and
                    (self.v_lower_limit <= v <= self.v_upper_limit))
        log.info(f"({u}, {v}) in bounds status is {inbounds}")
        return inbounds

    def stop(self):
        """
        Stops a move in progress on the controller.
        """
        self.send("ST")

    def reset(self):
        """
        Issue a hardware reset of the controller, equivalent to a power-up.
        """
        self.send("RS")

    def disable(self):
        """
        Disables the controller. Checks to ensure it has been disabled successfully
        """
        self.send("MM0")
        disabled = self.status()[1].split(" ")[0] == "DISABLE"
        if disabled:
            log.info(f"Successfully disabled the conex controller")
        else:
            log.warning(f"Unable to disable the conex controller!")

    def enable(self):
        """
        Enables the controller. Checks to ensure it has been enabled successfully
        """
        self.send("MM1")
        enabled = self.status()[1].split(" ")[0] == "READY"
        if enabled:
            log.info(f"Successfully enabled the conex controller")
        else:
            log.warning(f"Unable to enable the conex controller!")

    def format_msg(self, msg:str):
        """
        Overrides method from base class
        Command syntax is 'nnAAxx\r\n'
        nn - Controller number (typically 1 unless stages are daisy chained together)
        AA - Command name
        xx - Optional or required value or "?" to query current value

        If final characters of msg to not match self.terminator ('\r\n'), add the terminator
        If initial character does not match controller number
        """
        if msg and msg[-2:] != self.terminator:
            msg = msg+self.terminator
        if msg and ((msg[:1] != str(self.ctrlN)) or (msg[:2] != str(self.ctrlN))):
            msg = str(self.ctrlN) + msg
        return msg.encode('utf-8')

    def query(self, cmd: str, **kwargs):
        """
        Overrides method from base class
        Send command and wait for a response, kwargs passed to send, raises only IOError
        Response syntax is nnAAxx...xx
        nn - Controller number
        AA - Command name
        xx...xx - Response (of length dependent on the command sent).

        Checks to ensure the command is received with the proper syntax, removes qualifiers, and returns the query response
        """
        with self._rlock:
            try:
                self.send(cmd, **kwargs)
                time.sleep(.1)
                received = self.receive()
                cmd = cmd.rstrip("?")
                if (received[:1] == str(self.ctrlN)) or (received[:2] == str(self.ctrlN)):
                    received = received.lstrip(str(self.ctrlN))
                else:
                    raise IOError(f"Received inaccurate message from Conex!")
                if (received[:2] == cmd) or (received[:3] == cmd):
                    received = received.lstrip(cmd)
                else:
                    raise IOError(f"Received inaccurate message from Conex!")
                return received
            except Exception as e:
                raise IOError(e)