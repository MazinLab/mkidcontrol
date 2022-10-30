"""
Author: Joseph Redford Oct 21st 2022

Program to control ArduinoUNO that will turn on and off the laser diodes in the
calibration box. Code copied and modified from Noah's currentduinoAgent
"""

import serial
from serial import SerialException
import time
import logging
from logging import getLogger
import threading

from mkidcontrol.devices import SerialDevice
import mkidcontrol.mkidredis as redis
from mkidcontrol.mkidredis import RedisError

logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger(__name__)


names = ['808nm', '904nm', '980nm', '1120nm', '1310nm', 'mirror']


class Laserflipperduino(SerialDevice):
    VALID_FIRMWARES = (0.0, 0.1)

    def __init__(self, port, baudrate=115200, timeout=0., connect=True):
        super().__init__(port, baudrate, timeout, name='laserflipperduino')
        if connect:
            self.connect(raise_errors=False)
        self.status = [0, 0, 0, 0, 0, 0]
        self.terminator = ''

    def _postconnect(self):
        """
        Overwrites serialDevice _postconnect function. Sleeps for an appropriate amount of time to let the arduino get
        booted up properly so the first queries don't return nonsense (or nothing)
        """
        time.sleep(1)

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
                getLogger(__name__).debug(f"Sending '{msg}'")
                self.ser.write(msg)
            except (serial.SerialException, IOError) as e:
                self.disconnect()
                getLogger(__name__).error(f"...failed: {e}")
                raise e

    @property
    def firmware(self):
        """ Return the firmware string or raise IOError """
        try:
            log.debug(f"Querying currentduino firmware")
            response = self.query((7, 0), connect=True)
            version = float(response)
            return version
        except IOError as e:
            log.error(f"Serial error: {e}")
            raise e
        except ValueError:
            log.error(f"Bad firmware format: '{response}'")
            raise IOError(f'Bad firmware response: "{response}"')

    def firmware_ok(self):
        """ Return True or False if the firmware is supported, may raise IOErrors """
        return self.firmware in self.VALID_FIRMWARES

    def set_diode(self, index, value):
        """Set diode sets the pwm of a laser diode, the inputs are the diode
        index and a value of what fraction of current to apply

        index is the index of the diode going from 0 to 4 with the mapping
            defined by the names global list
        value is a value from 0 to 1 setting how much current to apply with 1
            being the max current defined by the resistors on the board
        """
        if (value < 0.) or (value > 1.):
            raise ValueError('invalid power setting')
        elif not isinstance(index, int) or (index < 0) or (index > 4):
            raise ValueError('invalid laser index')
        else:
            pwm_byte = int(value * 255)
            message = (index, pwm_byte)
            self.send(message)
            self.status[index] = pwm_byte
            reply = self.receive()
            getLogger(__name__).debug(reply.decode('utf-8'))

    def set_mirror_position(self, position):
        """sett_mirror_position takes a position argument to move the mirror
        flipper ot the right position

        position should be a numerical value, 0 moved the flipper down and a 
            non-zero value sets it to the up position
        """
        if position.lower() == 'down':
            log.debug(f"Setting mirror to down")
            byte_val = 0
        else:
            log.debug(f"Setting mirror to up")
            byte_val = 1
        message = (5, byte_val)
        response = self.query(message).split(':')
        val = response[1]
        self.status[5] = val
        if val == 0:
            log.info(f"Mirror flipped down")
        else:
            log.info(f"Mirror flipped up")

    def statuses(self):
        """get_status takes no arguments, prints the status of all 5 output
        pins"""
        self.send((6, 0))
        getLogger(__name__).debug("reading status of the arduino")
        status_reply = self.receive()
        status_reply = status_reply.split(',')
        for laser in status_reply:
            laser_dat = laser.split(':')
            name = names[int(laser_dat[0])]
            amp_value = int(laser_dat[1])
            self.status[int(laser_dat[0])] = amp_value

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


def callback(vals):
    keys = LASER_POWER_KEYS + MIRROR_POS_KEY
    d = {k: x for k, x in zip(keys, vals)}
    try:
        if all(i is None for i in vals):
            redis.store({STATUS_KEY: "Error"})
        else:
            redis.store(d, timeseries=True)
            redis.store({STATUS_KEY: "OK"})
    except RedisError:
        log.warning('Storing LakeShore336 data to redis failed!')


if __name__ == "__main__":

    redis.setup_redis(ts_keys=TS_KEYS)

    laserduino = Laserflipperduino(port='/dev/ttyACM0', baudrate=9600, timeout=0.1)

    time.sleep(1)
    laserduino.get_status()

