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


for k in MAG2_KEYS:
    a,b,c = k.split('.')
    c.devices[a].properties[b].elements[c].add_watcher(sort_key_change)


from mkidcore.fits import CalFactory, combineHDU, summarize


def get_obslog_record():



def get_fits_atom(live, obslog_record):

    im, start, et = live.receiveImage()

    ret = fits.ImageHDU(data=im)
    ret.header['utcstart'] = start
    ret.header['exptime'] = et
    ret.header['wmin'] = 'NaN'
    ret.header['wmax'] = 'NaN'
    ret.header['wavecal'] = live.wavecalID.decode('UTF-8', "backslashreplace")
    if ret.header['wavecal']:
        ret.header['wmin'] = live.wvlStart
        ret.header['wmax'] = live.wvlStop

    header.update(obslog_record)  #TODO



if __name__ == "__main__":
    redis.setup_redis(ts_keys=TS_KEYS)
    util.setup_logging('observingLogAgent')

    live = ImageCube(name=image, nRows=nRows, nCols=nCols,
                     useWvl=False, nWvlBins=1, wvlStart=False, wvlStop=False)

    while run:
        observation = redis.listen('observation:start', block=True).decode('json')
        target = observation['type']
        md = get_obslog_record()
        im = get_fits_atom(md)
        if target=='flat':
            dark = fits.open(redis.get(CURRENT_DARK_FILE_KEY))
            flatFac = CalFactory('flat', images=(im,), dark=dark)
            fn = redis.get(FLAT_FILE_TEMPLATE_KEY).format(md)
            badmask = fits.get_data(redis.get(BAD_PIXEL_MASK_KEY).format(md))
            flatFac.generate(fname=fn, badmask=badmask, save=True,
                             name=os.path.splitext(os.path.basename(fn))[0], overwrite=True)
            redis.store(CURRENT_FLAT_FILE_KEY, fn)
        elif target =='dark':
            darkFac.CalFactory('dark', images=(im,))
            fn = redis.get(DARK_FILE_TEMPLATE_KEY).format(md)
            badmask = fits.get_data(redis.get(BAD_PIXEL_MASK_KEY).format(md))
            darkFac.generate(fname=fn, badmask=badmask, save=True,
                             name=os.path.splitext(os.path.basename(fn))[0], overwrite=True)
            redis.store(CURRENT_DARK_FILE_KEY, fn)
        elif target in ('dwell', 'object'):
            fn = redis.get(SCI_FILE_TEMPLATE_KEY).format(md)
            dark = fits.open(redis.get(CURRENT_DARK_FILE_KEY))
            flat = fits.open(redis.get(CURRENT_FLAT_FILE_KEY))
            sciFac = CalFactory('sum', dark=dark, flat=flat)
            sciFac.addimage(im)
            sciFac.generate(threaded=True, fname=fn, name=observation['target'], save=True)




    # # Set up worker object and thread for the display.
    # #  All of this code could be axed if the live image was broken out into a separate program
    # cf = CalFactory('avg', images=self.imageList[-1:],
    #                 dark=self.darkField if self.checkbox_darkImage.isChecked() else None,
    #                 flat=self.flatField if self.checkbox_flatImage.isChecked() else None,
    #                 mask=self.beammapFailed)

    # cf = CalFactory('sum', images=self.imageList[-numImages2Sum:], dark=self.darkField if applyDark else None)
    # im = cf.generate(name='pixelcount')
    # pixelList = np.asarray(pixelList)
    # im.data[(pixelList[:, 1], pixelList[:, 0])].sum()
    #
    # self.sciFactory = CalFactory('sum', dark=self.darkField, flat=self.flatField)
