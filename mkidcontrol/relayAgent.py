"""
Author: Noah Swimmer, 9 September 2021

Program for communicating with and controlling the RLY-8 relay madule. Primary function of this module is to switch
power on/off for various different instruments/modules in the PICTURE-C electronics rack. It is also responsible for
reporting if the switches are on/off, and the other agents may use this information to check if the device that the
agent monitors is powered.

TODO: Test/validate
TODO: Generalize, move stuff over to devices.py
"""
import socket
import sys
import time
import threading
import logging
from logging import getLogger
import mkidcontrol.util as util
import mkidcontrol.pcredis as redis
from mkidcontrol.pcredis import RedisError


HOST = '192.168.10.101'
QUERY_INTERVAL = 1

COMMANDSRELAY = {'device-settings:relays:relay1': {'command': 'relay1', 'vals': {'on': 'on', 'off': 'off'}},
                 'device-settings:relays:relay2': {'command': 'relay2', 'vals': {'on': 'on', 'off': 'off'}},
                 'device-settings:relays:relay3': {'command': 'relay3', 'vals': {'on': 'on', 'off': 'off'}},
                 'device-settings:relays:relay4': {'command': 'relay4', 'vals': {'on': 'on', 'off': 'off'}},
                 'device-settings:relays:relay5': {'command': 'relay5', 'vals': {'on': 'on', 'off': 'off'}},
                 'device-settings:relays:relay6': {'command': 'relay6', 'vals': {'on': 'on', 'off': 'off'}},
                 'device-settings:relays:relay7': {'command': 'relay7', 'vals': {'on': 'on', 'off': 'off'}},
                 'device-settings:relays:relay8': {'command': 'relay8', 'vals': {'on': 'on', 'off': 'off'}},}

SETTING_KEYS = tuple(COMMANDSRELAY.keys())

STATUS_KEY = 'status:device:relay-module:status'
RELAY_STATUS_KEYS = [f"status:relays:relay{i}" for i in range(1,9)]

COMMAND_KEYS = [f"command:{k}" for k in SETTING_KEYS]

log = logging.getLogger(__name__)


class RelayModule:
    def __init__(self, host, port=2000, timeout=0.1):
        self.socket = None
        self.port = port
        self.host = host
        self.name = 'RelayModule'
        self.timeout = timeout
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
        time.sleep(2 * self.timeout)

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

        getLogger(__name__).debug(f"Connecting to {self.port} at {self.baudrate}")
        try:
            self._preconnect()
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.connect((self.host, self.port))
            self._postconnect()
            getLogger(__name__).info(f"Connection established to {self.host}:{self.port}")
            return True
        except Exception as e:
            self.ser = None
            getLogger(__name__).error(f"Conntecting to {self.host}:{self.port} failed: {e}")
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
            self.socket.close()
            self.socket = None
        except Exception as e:
            getLogger(__name__).info(f"Exception during disconnect: {e}")

    def relay_statuses(self):
        self.socket.send({"get": "relayStatus"})
        status = self.socket.recv(2048)
        return list(status.values())

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
                            if v is not None:
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


if __name__ == "__main__":

    util.setup_logging('RelayAgent')
    redis.setup_redis()
    relay = RelayModule(host=HOST, timeout=0.1)

    # ---------------------------------- MAIN OPERATION (The eternal loop) BELOW HERE ----------------------------------
    def callback(s):
        d = {k: x for k, x in zip(RELAY_STATUS_KEYS, s) if s}
        redis.store(d)
    relay.monitor(QUERY_INTERVAL, (relay.relay_statuses), value_callback=callback)

    while True:
        try:
            for key, val in redis.listen(COMMAND_KEYS):
                log.debug(f"relayAgent received {key}, {val}. Trying to send a command")
                if key in SETTING_KEYS:
                    cmd = COMMANDSRELAY[key]['command']
                    try:
                        assert val in ['on', 'off']
                        value = COMMANDSRELAY[key]['value'][val]
                    except AssertionError as e:
                        log.warning(f"Ignoring invalid command ('{key}={val}'): {e}")
                    try:
                        log.info(f"Processing command '{cmd}:{value}'")
                        relay.socket.send({cmd: value})
                        redis.store({key: value})
                        redis.store({STATUS_KEY: "OK"})
                    except IOError as e:
                        redis.store({STATUS_KEY: f"Error {e}"})
                        log.error(f"Comm error: {e}")
        except RedisError as e:
            log.critical(f"Redis server error! {e}")
            sys.exit(1)
