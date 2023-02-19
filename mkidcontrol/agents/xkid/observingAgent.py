#!/usr/bin/env python3
from logging import getLogger
import os
from mkidcontrol.mkidredis import RedisError
import mkidcontrol.mkidredis as redis
import mkidcontrol.util as util
from mkidcore.mkidcore.objects import Beammap
from mkidcore.mkidcore import metadata
from mkidcore.mkidcore.config import load as load_yaml_config
from astropy.io import fits
from mkidcore.fits import CalFactory, combineHDU, summarize
from magaoxindi.purepyindi.client import INDIClient
from mkidcontrol.packetmaster3 import Packetmaster
from datetime import datetime

BIN_FILE_DIR_KEY = ''
OBSERVING_REQUEST_CHANNEL = 'observation:request'
ACTIVE_DARK_FILE_KEY = ''  # a FQP to the active dark fits image, if any
ACTIVE_FLAT_FILE_KEY = ''  # a FQP to the active flat fits image, if any
SCI_FILE_TEMPLATE_KEY = ''  # FQP to image file as a template that will be formatted with metadata
DARK_FILE_TEMPLATE_KEY = ''  # FQP to  image file as a template that will be formatted with metadata
FLAT_FILE_TEMPLATE_KEY = ''  # FQP to  image file as a template that will be formatted with metadata
BAD_PIXEL_MASK_KEY = ''  # FQP to bad pixel mask file
GUI_LIVE_IMAGE_DEFAUTS_KEY = 'gui:live_image_config'
FITS_IMAGE_DEFAULTS_KEY = 'datasaver:fits_image_config'
INSTRUMENT_BEAMMAP_FILE_KEY = 'xkid:beammap:file'
GEN2_ROACHES_KEY = 'gen2:roaches'
GEN2_CAPTURE_PORT_KEY = 'gen2:capture_port'
GEN2_REDIS_MAP = {'dashboard.max_count_rate':'readout:coout_rate_limit',
                  'roaches':'roaches'
                  }

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


OBSLOG_RECORD_KEYS = { #This should be a superset of mkidcore.metadata.XKID_KEY_INFO
    'key': 'fitskey'  # redis keys to include in the fits header
}
# Include all the MAGAOX KEYS
OBSLOG_RECORD_KEYS.update({v[0]:v[1] for v in MAGAOX_KEYS.values()})


def get_obslog_record():
    """
    Grab all the data needed for an observation (as ultimately specified in the mkidcore.metadata.XKID_KEY_INFO)
    from redis using the OBSLOG_RECORD_KEYS dictionary and build them into a astropy.io.fits.Header suitable for
    logging or fits - file building
    """
    metadata.TIME_KEYS = ('MJD-END', 'MJD-STR', 'UT-END', 'UT-STR')
    metadata._time_key_builder = metadata._xkid_time_header
    kv_pairs = redis.read(list(OBSLOG_RECORD_KEYS.keys()), ts_value_only=True)
    fits_kv_pairs = [(OBSLOG_RECORD_KEYS[k], v) for k, v in kv_pairs]
    return metadata.build_header(fits_kv_pairs, use_simbad=False, KEY_INFO=metadata.XKID_KEY_INFO)


def gen2dashboard_yaml_to_redis(yaml, redis):
    """
    Loads a gen2 dashboard yaml config and put all the required things into the redis database at their appropriate
    keys

    #TODO this is very much a work in progress.
    """
    c = load_yaml_config(yaml)
    # TODO recurse or figure out how to handle dynamic things like the list of roaches
    #  or just require that everything be explicitly listed....might be best even if annoying
    redis.store(((redis_key, c.get(yaml_key)) for redis_key, yaml_key in GEN2_REDIS_MAP.items()))


def merge_start_stop_headers(header_start, header_stop):
    """Build a final observation header out of the start and stop headers"""
    start_keys = (,)  #TODO
    mid_keys = (,) #TODO
    for k in start_keys:
        header_stop[k]=header_start[k]
    for k in mid_keys:
        header_stop[k] = (header_start[k]+header_stop[k])/2
    return header_stop


def next_utc_second():
    """Return the next UTC second"""
    x = datetime.utcnow()
    x = round(x.hour * 3600 + x.minute * 60 + x.second + x.microsecond + .5)
    if x >= 24 * 3600:
        x = 0
    return x


if __name__ == "__main__":

    args = parse_args()
    util.setup_logging('observingAgent')
    redis.setup_redis(ts_keys=TS_KEYS)  # TODO doesn't this redis instance need to be aware of ALL the potential TS keys?

    indi = INDIClient('localhost', 7624)  # TODO add error handling and autorecovery
    indi.start()

    def indi2redis(element, changed):
        if changed:
            indikey = f'{element.property.device.name}.{element.property.name}.{element.name}'
            redis.store(MAGAOX_KEYS[indikey][0], element.value)

    for k in MAGAOX_KEYS:
        a, b, c = k.split('.')
        c.devices[a].properties[b].elements[c].add_watcher(indi2redis)

    gen2dashboard_yaml_to_redis(args.dashboard_yaml)

    default = dict(nRows=100, nCols=100, useWvl=False, nWvlBins=1, useEdgeBins=False, wvlStart=0.0, wvlStop=0.0)
    livecfg = redis.read(GUI_LIVE_IMAGE_DEFAUTS_KEY, decode_json=True) or default
    fitscfg = redis.read(FITS_IMAGE_DEFAULTS_KEY, decode_json=True) or default
    beammap = Beammap(redis.read(INSTRUMENT_BEAMMAP_FILE_KEY, error_missing=True))
    n_roaches = len(redis.read(GEN2_ROACHES_KEY, error_missing=True))  # TODO @nswimmer
    port = redis.read(GEN2_CAPTURE_PORT_KEY, error_missing=True)

    pm = Packetmaster(n_roaches, port, useWriter=not args.offline,
                      sharedImageCfg={'live': livecfg, 'fits': fitscfg},
                      beammap=beammap, forwarding=False, recreate_images=True)
    fits_imagecube = pm.sharedImages['fits']

    limitless_integration = False
    fits_exp_time = None

    mask, maskfile = None, None
    dark, darkfile = None, None
    flat, flatfile = None, None
    request = {'type':'abort'}
    try:
        while True:
            if not limitless_integration:
                request = redis.listen(OBSERVING_REQUEST_CHANNEL, value_only=True, decode='json')
                if request['type'] == 'abort':
                    getLogger(__name__).debug(f'Request to stop while nothing in progress.')
                    pm.stopWriting()
                    continue

                limitless_integration = request['duration'] == 'inf'
                fits_exp_time = 60 if limitless_integration else request['duration']

                pm.startWriting(redis.read(BIN_FILE_DIR_KEY, decode_json=False))
                fits_imagecube.startIntegration(startTime=next_utc_second(), integrationTime=fits_exp_time)
                md_start = get_obslog_record()

            try:
                #TODO need a way to listen with a timeout for this arch. otherwise will need to move listen to a
                # thread
                #TODO this also won't work well with the non-deterministice elampse time for saved each 60s exposure in
                # limitless mode. Need some thought here.
                # basically we want to be listening for abort as much as possible not waiting to receive the image
                request = redis.listen(OBSERVING_REQUEST_CHANNEL, timeout=fits_exp_time-.2, value_only=True, decode='json')
            except TimeoutError:
                pass
            else:
                if request['type'] != 'abort':
                    getLogger(__name__).warning(f'Ignoring observation request because one is already in progress')
                else:
                    #Stop writing photons, no need to touch the imagecube
                    pm.stopWriting()
                    limitless_integration = False
                    continue

            im_data, start_t, expo_t = fits_imagecube.receiveImage(timeout=False, return_info=True)
            md_end = get_obslog_record()
            header = merge_start_stop_headers(md_start, md_end)
            if limitless_integration:
                fits_imagecube.startIntegration(startTime=0, integrationTime=fits_exp_time)
                md_start = get_obslog_record()

            ret = fits.ImageHDU(data=im_data, header=header)
            ret.data
            ret.header['wavecal'] = fits_imagecube.wavecalID.decode('UTF-8', "backslashreplace")
            ret.header['wmin'] = fits_imagecube.wvlStart
            ret.header['wmax'] = fits_imagecube.wvlStop
            header_dict = dict(ret.header)

            def load_if_necessary(data, file, key):
                new = redis.read(key)
                try:
                    if new != file:
                        file = new
                        data = fits.getdata(file)
                except IOError:
                    data = None
                    file = None
                return data, file


            mask, maskfile = load_if_necessary(mask, maskfile, BAD_PIXEL_MASK_KEY)
            if request['type'] == 'flat':
                dark, darkfile = load_if_necessary(dark, darkfile, ACTIVE_DARK_FILE_KEY)
                fn = redis.read(FLAT_FILE_TEMPLATE_KEY).format(**header_dict)

                flatFac = CalFactory('flat', images=(ret.data,), dark=dark)
                flat = flatFac.generate(fname=fn, badmask=mask, save=True,
                                         name=os.path.splitext(os.path.basename(fn))[0], overwrite=True)
                flatfile = fn
                redis.store(ACTIVE_FLAT_FILE_KEY, fn)
            elif request['type'] == 'dark':
                darkFac = CalFactory('dark', images=(ret.data,))
                fn = redis.read(DARK_FILE_TEMPLATE_KEY).format(**header_dict)
                dark = darkFac.generate(fname=fn, badmask=mask, save=True,
                                 name=os.path.splitext(os.path.basename(fn))[0], overwrite=True)
                darkfile = fn
                redis.store(ACTIVE_DARK_FILE_KEY, fn)
            elif request['type'] in ('dwell', 'object'):
                flat, flatfile = load_if_necessary(flat, flatfile, ACTIVE_FLAT_FILE_KEY)
                dark, darkfile = load_if_necessary(dark, darkfile, ACTIVE_DARK_FILE_KEY)
                fn = redis.read(SCI_FILE_TEMPLATE_KEY).format(**header_dict)
                sciFac = CalFactory('avg', images=(ret.datam,), dark=dark, flat=flat)
                sciFac.generate(threaded=True, fname=fn, name=request['name'], save=True, overwrite=True)

            if not limitless_integration:
                pm.stopWriting()
    except Exception:
        pm.quit()
        indi.stop()

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
