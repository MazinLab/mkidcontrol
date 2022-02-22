"""
Author: Noah Swimmer
8 October 2020

Program for communicating with and controlling the LakeShore240 Thermometry Unit.
This module is responsible for reading out 2 temperatures, that of the LN2 tank and the LHe tank.
Both are identical LakeShore DT-670A-CU diode thermometers. Using the LakeShore MeasureLink desktop application, the
LakeShore can be configured easily (it autodetects the thermometers and loads in the default calibration curve). There
will be functionality in the lakeshore240Agent to configure settings, although that should not be necessary unless the
thermometers are removed and replaced with new ones.
Again, the calibration process can be done manually using the LakeShore GUI if so desired.

HARDWARE NOTE: Ch1 -> LN2, Ch2 -> LHe

See manual in hardware/thermometry/LakeShore240_temperatureMonitor_manual.pdf
"""

import sys
import time
import logging
import threading

import mkidcontrol.devices
from mkidcontrol.pcredis import PCRedis, RedisError
import mkidcontrol.util as util


LAKESHORE240_KEYS = ['status:temps:lhetank',
        'status:temps:ln2tank',
        'status:device:ls240:firmware',
        # 'status:device:ls240:status',
        'status:device:ls240:model',
        'status:device:ls240:sn']

TS_KEYS = ['status:temps:lhetank', 'status:temps:ln2tank']

# STATUS_KEY = "status:device:ls240:status"

FIRMWARE_KEY = "status:device:ls240:firmware"
MODEL_KEY = 'status:device:ls240:model'
SN_KEY = 'status:device:ls240:sn'

QUERY_INTERVAL = 1

log = logging.getLogger()


class LakeShore240(mkidcontrol.devices.SerialDevice):
    def __init__(self, name, port, baudrate=115200, timeout=0.1, connect=True):
        super().__init__(port, baudrate, timeout, name=name)

        self._monitor_thread = None  # Maybe not even necessary since this only queries
        self.last_he_temp = None
        self.last_ln2_temp = None
        self.terminator = '\n'

        self.sn = None
        self.firmware = None
        self.enabled_channels = None

        if connect:
            self.connect(raise_errors=False)

    def format_msg(self, msg:str):
        """
        Overrides agent.SerialDevice format_message() function. Commands to the LakeShore 240 are all upper-case.
        *NOTE: By choice, using .upper(), if we manually store a name of a curve/module, it will be in all caps.
        """
        return super().format_msg(msg.strip().upper())

    def _postconnect(self):
        VALID_MODELS = ("MODEL240-2P", "MODEL240-8P")

        id_msg = self.query("*IDN?")
        try:
            manufacturer, model, self.sn, self.firmware = id_msg.split(",")
        except ValueError:
            log.debug(f"Unable to parse IDN response: '{id_msg}'")
            manufacturer, model, self.sn, self.firmware = [None]*4

        if not (manufacturer == "LSCI") and (model in VALID_MODELS):
            msg = f"Unsupported device: {manufacturer}/{model} (idn response = '{id_msg}')"
            log.critical(msg)
            raise IOError(msg)

        self.name += f"-{model[-2:]}"

        enabled = []
        for channel in range(1, int(model[-2]) + 1):
            try:
                _, _, enabled_status = self.query(f"INTYPE? {channel}").rpartition(',')
                if enabled_status == "1":
                    enabled.append(channel)
            except IOError as e:
                log.error(f"Serial error: {e}")
                raise IOError(f"Serial error: {e}")
            except ValueError:
                log.critical(f"Channel {channel} returned and unknown value from channel information query")
                raise IOError(f"Channel {channel} returned and unknown value from channel information query")
        self.enabled_channels = tuple(enabled)

    @property
    def device_info(self):
        self.connect()
        return dict(model=self.name, firmware=self.firmware, sn=self.sn, enabled=self.enabled_channels)

    def read_temperatures(self):
        """Queries the temperature of all enabled channels on the LakeShore 240. LakeShore reports values of temperature
        in Kelvin. May raise IOError in the case of serial communication not working."""
        readings = []
        tanks = ['ln2', 'lhe']
        for channel in self.enabled_channels:
            try:
                readings.append(float(self.query(f"KRDG? {channel}")))
            except IOError as e:
                log.error(f"Serial Error: {e}")
                raise IOError(f"Serial Error: {e}")
            except ValueError as e:
                log.error(f"Parsing error: {e}")
                raise ValueError(f"Parsing error: {e}")
        temps = {tanks[i]: readings[i] for i in range(len(self.enabled_channels))}
        return temps

    def _set_curve_name(self, channel: int, name: str):
        """Engineering function to set the name of a curve on the LakeShore240. Convenient since both thermometers are
        DT-670A-CU style, and so this can clear any ambiguity. Does not need to be used in normal operation. Logs
        IOError but does not raise it.
        """
        try:
            self.send(f'INNAME{str(channel)},"{name}"')
        except IOError as e:
            log.error(f"Unable to set channel {channel}'s name to '{name}'. "
                      f"Check to make sure the LakeShore USB is connected!")


if __name__ == "__main__":

    util.setup_logging('lakeshore240Agent')
    redis = PCRedis(create_ts_keys=TS_KEYS)
    lakeshore = LakeShore240(name='LAKESHORE240', port='/dev/lakeshore', baudrate=115200, timeout=0.1)

    try:
        info = lakeshore.device_info
        # Note that placing the store before exit makes this program behave differently in an abort
        #  than both of the sims, which would not alter the database. I like this better.
        redis.store({FIRMWARE_KEY: info['firmware'], MODEL_KEY: info['model'], SN_KEY: info['sn']})
    except IOError as e:
        log.error(f"When checking device info: {e}")
        redis.store({FIRMWARE_KEY: '',  MODEL_KEY: '', SN_KEY: ''})
        sys.exit(1)
    except RedisError as e:
        log.critical(f"Redis server error! {e}")
        sys.exit(1)

    while True:
        try:
            temps = lakeshore.read_temperatures()
            redis.store({'status:temps:ln2tank': temps['ln2'],
                         'status:temps:lhetank': temps['lhe']}, timeseries=True)
        except (IOError, ValueError) as e:
            log.error(f"Communication with LakeShore 240 failed: {e}")
        except RedisError as e:
            log.critical(f"Redis server error! {e}")
            sys.exit(1)
        time.sleep(QUERY_INTERVAL)
