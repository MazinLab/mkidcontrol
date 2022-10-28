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


logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger(__name__)

names = ['808nm', '920nm', '980nm', '1120nm', '1310nm', 'mirror']

from mkidcontrol.devices import SerialDevice

class LaserFlipperduino(SerialDevice):
    def __init__(self, port, baudrate=9600, timeout=0., connect=True):
        super().__init__(port, baudrate, timeout, name='laserflipperduino')
        if connect:
            self.connect(raise_errors=False)
        self.status = [0, 0, 0, 0, 0, 0]

    def _postconnect(self):
        """
        Overwrites serialDevice _postconnect function. Sleeps for an appropriate amount of time to let the arduino get
        booted up properly so the first queries don't return nonsense (or nothing)
        """
        time.sleep(1)

    def disconnect(self):
        try:
            self.ser.close()
            self.ser = None
        except Exception as e:
            getLogger(__name__).info(f"Exception durring disconnect: {e}")

    def send(self, msg: bytearray, connect=True):
        if connect:
            self.connect()
        try:
            getLogger(__name__).debug(f"writing message: {msg}")
            self.ser.write(msg)
            getLogger(__name__).debug(f"Sent {msg} successfully")
        except (SerialException, IOError) as e:
            self.disconnect()
            getLogger(__name__).error(f"Send failed: {e}")
            # raise e

    def receive(self):
        try:
            data = self.ser.readline().decode("utf-8").strip()
            getLogger(__name__).debug(f"read {data} from arduino")
            return data
        except (SerialException, IOError) as e:
            self.disconnect()
            getLogger(__name__).debug(f"Send failed: {e}")
            # raise e

    def set_diode(self, index, value):
        """Set diode sets the pwm of a laser diode, the inputs are the diode
        index and a value of what fraction of current to apply

        index is the index of the diode going from 0 to 4 with the mapping
            defined by the names global list
        value is a value from 0 to 1 setting how much current to apply with 1
            being the max current defined by the resistors on the board
        """
        if ((value < 0.) or (value > 1.)):
            raise ValueError('invalid power setting')
        elif (not isinstance(index, int) or (index < 0) or (index > 4)):
            raise ValueError('invalid laser index')
        else:
            pwm_byte = int(value * 255)
            message = bytearray([index, pwm_byte])
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
        if position == 0.:
            byte_val = 0
        else:
            byte_val = 1
        message = bytearray([5, byte_val])
        self.send(message)
        self.status[5] = byte_val
        reply = self.receive()
        getLogger(__name__).debug(reply.decode('utf-8'))

    def get_status(self):
        """get_status takes no arguments, prints the status of all 5 output
        pins"""
        self.send(bytearray([6, 0]))
        getLogger(__name__).debug("reading status of the arduino")
        total_msg = []
        time.sleep(0.3)
        for i in range(6):
            reply = self.ser.readline().decode("utf-8").strip()
            reply = reply.split(':')
            name_value = names[int(reply[0])]
            amp_value = int(reply[1])
            self.status[int(reply[0])] = amp_value
            msg = ':'.join([name_value, reply[1]])
            total_msg.append(msg)
        total_msg = '\n'.join(total_msg)
        print(total_msg)
        #getLogger(__name__).debug(total_msg)


if __name__ == "__main__":

    laserduino = Laserduino(port='/dev/ttyACM0', baudrate=9600, timeout=0.1)

    time.sleep(1)
    laserduino.get_status()

