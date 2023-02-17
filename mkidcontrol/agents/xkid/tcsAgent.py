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
"""

import logging
import sys

from mkidcontrol.mkidredis import RedisError
import mkidcontrol.mkidredis as redis
import mkidcontrol.util as util
from mkidcontrol.commands import COMMANDSFOCUS, LakeShoreCommand
from mkidcontrol.devices import Focus

logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger(__name__)

QUERY_INTERVAL = 1

ENCODER_STEPS_PER_MM = 34555

STATUS_KEY = "status:device:focus:status"

FOCUS_POSITION_MM_KEY = 'status:device:focus:position:mm'
FOCUS_POSITION_ENCODER_KEY = 'status:device:focus:position:encoder'

TS_KEYS = (FOCUS_POSITION_MM_KEY, FOCUS_POSITION_ENCODER_KEY)


from magaoxindi.purepyindi.client import INDIClient
c = INDIClient('localhost', 7624)
c.start()

MAG2_KEYS = {
'tcsi.telpos.am',
'tcsi.telpos.dec',
'tcsi.telpos.el',
'tcsi.telpos.epoch',
'tcsi.telpos.ha',
'tcsi.telpos.ra',
'tcsi.telpos.rotoff',
'tcsi.teldata.az',
'tcsi.teldata.dome_az',
'tcsi.teldata.dome_stat',
'tcsi.teldata.guider_moving',
'tcsi.teldata.guiding',
'tcsi.teldata.pa',
'tcsi.teldata.roi',
'tcsi.teldata.slewing',
'tcsi.teldata.tracking',
'tcsi.teldata.zd',
'tcsi.teltime.sidereal_time',
}

REDIS_KEYS = (

)



def sort_key_change(element, did_anything_change):
    if did_anything_change:
        def sort_key_change(element, did_anything_change):

            redis.store(exlemnt,dro)
        print(f'{element.property.device.name}.{element.property.name}.{element.name} was just updated to {element.value}')


sort_key_change

def get_fits_keys():


for k in MAG2_KEYS:
    a,b,c = k.split('.')
    c.devices[a].properties[b].elements[c].add_watcher(sort_key_change)



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
                    elif key == HOME_KEY:
                        f.home()
                except IOError as e:
                    redis.store({STATUS_KEY: f"Error {e}"})
                    log.error(f"Comm error: {e}")
    except RedisError as e:
        log.critical(f"Redis server error! {e}")
        sys.exit(1)
