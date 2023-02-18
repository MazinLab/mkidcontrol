#!/usr/bin/env python3
import logging
import sys

from mkidcontrol.mkidredis import RedisError
import mkidcontrol.mkidredis as redis
import mkidcontrol.util as util
from mkidcore.fits import CalFactory, combineHDU, summarize
from magaoxindi.purepyindi.client import INDIClient

OBSERVING_REQUEST_CHANNEL = 'observation:request'
ACTIVE_DARK_FILE_KEY = '' # a FQP to the active dark fits image, if any
ACTIVE_FLAT_FILE_KEY = '' # a FQP to the active flat fits image, if any
SCI_FILE_TEMPLATE_KEY = ''  #FQP to image file as a template that will be formatted with metadata
DARK_FILE_TEMPLATE_KEY = ''  #FQP to  image file as a template that will be formatted with metadata
FLAT_FILE_TEMPLATE_KEY = ''  #FQP to  image file as a template that will be formatted with metadata
BAD_PIXEL_MASK_KEY = '' #FQP to bad pixel mask file
GUI_LIVE_IMAGE_DEFAUTS_KEY = 'gui:live_image_config'
FITS_IMAGE_DEFAULTS_KEY = 'datasaver:fits_image_config'
INSTRUMENT_BEAMMAP_FILE_KEY = 'xkid:beammap:file'
GEN2_ROACHES_KEY='gen2:roaches'
GEN2_CAPTURE_PORT_KEY= 'gen2:capture_port'


MAGAOX_KEYS = {
'tcsi.telpos.am': ('rediskey', 'fitskey', 'description'),
'tcsi.telpos.dec': ('rediskey', 'fitskey', 'description'),
'tcsi.telpos.el': ('rediskey', 'fitskey', 'description'),
'tcsi.telpos.epoch': ('rediskey', 'fitskey', 'description'),
'tcsi.telpos.ha': ('rediskey', 'fitskey', 'description'),
'tcsi.telpos.ra': ('rediskey', 'fitskey', 'description'),
'tcsi.telpos.rotoff': ('rediskey', 'fitskey', 'description'),
'tcsi.teldata.az': ('rediskey', 'fitskey', 'description'),
'tcsi.teldata.dome_az': ('rediskey', 'fitskey', 'description'),
'tcsi.teldata.dome_stat': ('rediskey', 'fitskey', 'description'),
'tcsi.teldata.guider_moving': ('rediskey', 'fitskey', 'description'),
'tcsi.teldata.guiding': ('rediskey', 'fitskey', 'description'),
'tcsi.teldata.pa': ('rediskey', 'fitskey', 'description'),
'tcsi.teldata.roi': ('rediskey', 'fitskey', 'description'),
'tcsi.teldata.slewing': ('rediskey', 'fitskey', 'description'),
'tcsi.teldata.tracking': ('rediskey', 'fitskey', 'description'),
'tcsi.teldata.zd': ('rediskey', 'fitskey', 'description'),
'tcsi.teltime.sidereal_time': ('rediskey', 'fitskey', 'description'),
}


OBSLOG_RECORD_KEYS = {
    'key': ('fitskey', 'description') #redis keys to include in the fits header
}


def get_obslog_record():
    #TODO need to handle timeseries keys
    return redis.get(list(OBSLOG_RECORD_KEYS.keys()))


def gen2dashboard_yaml_to_redis(yaml, redis):
    """Loads a gen2 dashboard yaml config and puts all the required things into the redis database"""
    #TODO
    pass


if __name__ == "__main__":

    util.setup_logging('observingAgent')
    redis.setup_redis(ts_keys=TS_KEYS)

    c = INDIClient('localhost', 7624)  #TODO add error handling and autorecovery
    c.start()

    def indi2redis(element, changed):
        if changed:
            indikey=f'{element.property.device.name}.{element.property.name}.{element.name}'
            redis.store(MAGAOX_KEYS[indikey][0], element.value)
            msg = f'{indikey} was just updated to {element.value}'
            getLogger(__name__).debug(msg)


    for k in MAGAOX_KEYS:
        a, b, c = k.split('.')
        c.devices[a].properties[b].elements[c].add_watcher(indi2redis)

    gen2dashboard_yaml_to_redis(args.dashboard_yaml)

    livecfg = redis.get(GUI_LIVE_IMAGE_DEFAUTS_KEY,{})
    fitscfg = redis.get(FITS_IMAGE_DEFAULTS_KEY,{})
    beammap = Beammap(redis.get(INSTRUMENT_BEAMMAP_FILE_KEY))
    n_roaches = len(redis.get(GEN2_ROACHES_KEY))
    port = len(redis.get(GEN2_CAPTURE_PORT_KEY))


    pm = Packetmaster(n_roaches, port, useWriter=not args.offline,
                                sharedImageCfg={'live': livecfg, 'fits': fitscfg},
                                beammap=beammap, forwarding=False, recreate_images=True)
    fits_imagecube = pm.sharedImages['fits']

    while True:
        request = redis.listen(OBSERVING_REQUEST_CHANNEL, block=True).decode('json')
        if request['type']==stop:
            get_logger(__name__).debug(f'Request to stop while nothing in progress.')
            pm.write()
            continue

        #TODO handle observation['duration'] is infinite case

        fits_imagecube.startIntegration(observation['duration'])
        md = get_obslog_record()
        try:
            request = redis.listen(OBSERVING_REQUEST_CHANNEL, timeout=observation['duration']).decode('json')
        except TimeoutError:
            pass
        else:
            if request['type'] != 'stop':
                get_logger(__name__).warning(f'Ignoring observation request because one is already in progress')
            else:
                pm.stop()
                fits_imagecube.reset()
                continue

        im_data = fits_imagecube.recieveImage(block=True)
        ret = fits.ImageHDU(data=im_data)
        # ret.header['utcstart'] = start
        # ret.header['exptime'] = et
        # ret.header['wmin'] = 'NaN'
        # ret.header['wmax'] = 'NaN'
        header.update(obslog_record)  # TODO
        ret.header['wavecal'] = fits_imagecube.wavecalID.decode('UTF-8', "backslashreplace")
        if ret.header['wavecal']:
            ret.header['wmin'] = fits_imagecube.wvlStart
            ret.header['wmax'] = fits_imagecube.wvlStop


        if observation['type']=='flat':
            dark = fits.open(redis.get(CURRENT_DARK_FILE_KEY))
            flatFac = CalFactory('flat', images=(im,), dark=dark)
            fn = redis.get(FLAT_FILE_TEMPLATE_KEY).format(md)
            badmask = fits.get_data(redis.get(BAD_PIXEL_MASK_KEY).format(md))
            flatFac.generate(fname=fn, badmask=badmask, save=True,
                             name=os.path.splitext(os.path.basename(fn))[0], overwrite=True)
            redis.store(CURRENT_FLAT_FILE_KEY, fn)
        elif observation['type'] =='dark':
            darkFac.CalFactory('dark', images=(im,))
            fn = redis.get(DARK_FILE_TEMPLATE_KEY).format(md)
            badmask = fits.get_data(redis.get(BAD_PIXEL_MASK_KEY).format(md))
            darkFac.generate(fname=fn, badmask=badmask, save=True,
                             name=os.path.splitext(os.path.basename(fn))[0], overwrite=True)
            redis.store(CURRENT_DARK_FILE_KEY, fn)
        elif observation['type'] in ('dwell', 'object'):
            fn = redis.get(SCI_FILE_TEMPLATE_KEY).format(md)
            dark = fits.open(redis.get(CURRENT_DARK_FILE_KEY))
            flat = fits.open(redis.get(CURRENT_FLAT_FILE_KEY))
            sciFac = CalFactory('avg', dark=dark, flat=flat)
            sciFac.addimage(im)
            sciFac.generate(threaded=True, fname=fn, name=observation['name'], save=True, overwrite=True)

        pm.stop()



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