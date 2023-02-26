#!/usr/bin/env python3
from logging import getLogger
import os
import argparse
from mkidcontrol.mkidredis import RedisError
import mkidcontrol.mkidredis as redis
import mkidcontrol.util as util
from mkidcore.objects import Beammap
from mkidcore import metadata
from mkidcore import utils as mkcu
from mkidcore.config import load as load_yaml_config
from astropy.io import fits
from mkidcore.fits import CalFactory
from magaoxindi.purepyindi.client import INDIClient
from mkidcontrol.packetmaster3 import Packetmaster
from mkidcontrol.config import REDIS_TS_KEYS

metadata.TIME_KEYS = ('MJD-END', 'MJD-STR', 'UT-END', 'UT-STR')
metadata._time_key_builder = metadata._xkid_time_header

TS_KEYS = REDIS_TS_KEYS

DASHBOARD_YAML_KEY = 'xkid:configuration:file:yaml:dashboard'
CFG_DIR_KEY = 'xkid:configuration:directory'
BIN_FILE_DIR_KEY = 'xkid:configuration:directory:bin-files'
OBSERVING_REQUEST_CHANNEL = 'command:observation:request'
ACTIVE_DARK_FILE_KEY = 'xkid:configuration:file:dark:active'  # a FQP to the active dark fits image, if any
ACTIVE_FLAT_FILE_KEY = 'xkid:configuration:file:flat:active'  # a FQP to the active flat fits image, if any
SCI_FILE_TEMPLATE_KEY = 'xkid:configuration:file:sci:template'  # FQP to image file as a template that will be formatted with metadata
DARK_FILE_TEMPLATE_KEY = 'xkid:configuration:file:dark:template'  # FQP to  image file as a template that will be formatted with metadata
FLAT_FILE_TEMPLATE_KEY = 'xkid:configuration:file:flat:template'  # FQP to  image file as a template that will be formatted with metadata
BAD_PIXEL_MASK_KEY = 'xkid:configuration:file:mask:bad-pixel'  # FQP to bad pixel mask file
GUI_LIVE_IMAGE_DEFAUTS_KEY = 'gui:live_image_config'
FITS_IMAGE_DEFAULTS_KEY = 'datasaver:fits_image_config'
INSTRUMENT_BEAMMAP_FILE_KEY = 'xkid:configuration:file:beammap'
GEN2_ROACHES_KEY = 'gen2:roaches'
GEN2_CAPTURE_PORT_KEY = 'gen2:capture_port'
GEN2_REDIS_MAP = {'dashboard.max_count_rate':'readout:count_rate_limit',
                  'roaches':'gen2:roaches',
                  'packetmaster.captureport':'gen2:capture_port'}


MAGAOX_KEYS = {
    'tcsi.telpos.am': ('tcsi:telpos:am', 'AIRMASS', 'Airmass at start'),
    'tcsi.telpos.dec': ('tcsi:telpos:dec', 'DEC', 'DEC of telescope pointing (+/-DD:MM:SS.SS)'),
    'tcsi.telpos.el': ('tcsi:telpos:el', 'ALTITUDE', 'Elevation of telescope pointing'), # Altitude?
    'tcsi.telpos.epoch': ('tcsi:telpos:epoch', 'EPOCH', 'Epoch of observation from MagAO-X'),
    'tcsi.telpos.ha': ('tcsi:telpos:ha', 'HA', 'description'), # TODO: Not sure?
    'tcsi.telpos.ra': ('tcsi:telpos:ra', 'RA', 'RA of telescope pointing (HH:MM:SS.SSS)'),
    'tcsi.telpos.rotoff': ('tcsi:telpos:rotoff', 'ROT_STAT', 'Telescope rotator on/off'),
    'tcsi.teldata.az': ('tcsi:teldata:az', 'AZIMUTH', 'Azimuth of telescope pointing'),
    'tcsi.teldata.dome_az': ('tcsi:teldata:dome-az', 'DOM-AZ', 'Azimuth of dome'),
    'tcsi.teldata.dome_stat': ('tcsi:teldata:dome-state', 'DOM-STAT', 'State of the dome at exposure start time'),
    'tcsi.teldata.guider_moving': ('tcsi:teldata:guider-moving', 'fitskey', 'Telescope status if guider is moving'),
    'tcsi.teldata.guiding': ('tcsi:teldata:guiding', 'fitskey', 'Telescope guiding status'),
    'tcsi.teldata.pa': ('tcsi:teldata:pa', 'fitskey', 'Position Angle'), # TODO: Position angle of what?
    'tcsi.teldata.roi': ('tcsi:teldata:roi', 'fitskey', 'description'), # TODO: Not sure
    'tcsi.teldata.slewing': ('tcsi:teldata:slewing', 'SLEWING', 'Telescope slewing status'),
    'tcsi.teldata.tracking': ('tcsi:teldata:tracking', 'TRACKING', 'Telescope tracking status'),
    'tcsi.teldata.zd': ('tcsi:teldata:zd', 'ZD', 'Zenith distance at typical time'),
    'tcsi.teltime.sidereal_time': ('tcsi:teldata:sidereal-time', 'SID-TIME', 'Sidereal time at typical time'), # TODO: Sidereal time at start and end?
    'tcsi.environment.dewpoint' : ('tcsi:environment:dewpoint', 'DOM-DEW', 'Dewpoint'),
    'tcsi.environment.humidity' : ('tcsi:environment:humidity', 'DOM-HUM', 'Humidity'),
    'tcsi.environment.pressure' : ('tcsi:environment:pressure', 'DOM-PRS', 'Pressure (hpa)'),
    'tcsi.environment.temp-amb' : ('tcsi:environment:temp-amb', 'DOM-TMPA', 'Ambient temperature'),
    'tcsi.environment.temp-cell' : ('tcsi:environment:temp-cell', 'DOM-TMPC', 'desc'), # TODO: Not sure
    'tcsi.environment.temp-out' : ('tcsi:environment:temp-out', 'DOM-TMPO', 'Outside temperature'),
    'tcsi.environment.temp-seccell' : ('tcsi:environment:temp-seccell', 'DOM-TMPS', 'desc'), # TODO: Not sure
    'tcsi.environment.temp-truss' : ('tcsi:environment:temp-truss', 'DOM-TMPT', 'Truss temperature'),
    'tcsi.environment.wind' : ('tcsi:environment:wind', 'DOM-WND', 'Wind speed'),
    'tcsi.environment.winddir' : ('tcsi:environment:winddir', 'DOM-WNDD', 'Wind direction'),
    'tcsi.catalog.object' : ('tcsi:catalog:object', 'OBJECT', 'Object'),
    'tcsi.catalog.rotmode' : ('tcsi:catalog:rotmode', 'fits', 'desc'), # TODO: Not sure
    'tcsi.catdata.dec' : ('tcsi:catdata:dec', 'fits', 'desc'), # TODO: Not sure
    'tcsi.catdata.epoch' : ('tcsi:catdata:epoch', 'fits', 'desc'), # TODO: Not sure
    'tcsi.catdata.ra' : ('tcsi:catdata:ra', 'fits', 'desc'), # TODO: Not sure
    'tcsi.catdata.rotoff' : ('tcsi:catdata:rotoff', 'fits', 'desc'), # TODO: Not sure
    'tcsi.seeing.dimm_el' : ('tcsi:seeing:dimm-el', 'DIMM-EL', 'DIMM elevation'),
    'tcsi.seeing.dimm_fwhm' : ('tcsi:seeing:dimm-fwhm', 'DIMM-SEE', 'DIMM seeing (FWHM)'),
    'tcsi.seeing.dimm_fwhm_corr' : ('tcsi:seeing:dimm-fwhm-corr', 'DIMM-COR', 'desc'), # TODO: Not sure
    'tcsi.seeing.dimm_time' : ('tcsi:seeing:dimm-time', 'DIMM-TIM', 'desc'), # TODO: Not sure
    'tcsi.seeing.mag1_el' : ('tcsi:seeing:mag1-el', 'MAG1-EL', 'Mag1 elecation'),
    'tcsi.seeing.mag1_fwhm' : ('tcsi:seeing:mag1-fwhm', 'MAG1-SEE', 'Mag1 seeing (FWHM)'),
    'tcsi.seeing.mag1_fwhm_corr' : ('tcsi:seeing:mag1-fwhm-corr', 'MAG1-COR', 'desc'), # TODO: Not sure
    'tcsi.seeing.mag1_time' : ('tcsi:seeing:mag1-time', 'MAG1-TIM', 'desc'), # TODO: Not sure
    'tcsi.seeing.mag2_el' : ('tcsi:seeing:mag2-el', 'MAG2-EL', 'Mag2 elevation'),
    'tcsi.seeing.mag2_fwhm' : ('tcsi:seeing:mag2-fwhm', 'MAG2-SEE', 'Mag2 seeing (FWHM)'),
    'tcsi.seeing.mag2_fwhm_corr' : ('tcsi:seeing:mag2-fwhm-corr', 'MAG2-COR', 'desc'), # TODO: Not sure
    'tcsi.seeing.mag2_time' : ('tcsi:seeing:mag2-time', 'MAG2-TIM', 'desc'), # TODO: Not sure
}

OBSLOG_RECORD_KEYS = { #This should be a superset of mkidcore.metadata.XKID_KEY_INFO
    'rediskey': 'fitskey',  # redis keys to include in the fits header
    'xkid:configuration:observation:key:device-temperature' : 'DET-TMP',
    'xkid:configuration:observation:key:beammap' : 'BMAP',
    'xkid:configuration:observation:key:config-directory' : 'CFGDIR',
    'xkid:configuration:observation:key:conex-x' : 'CONEXX',
    'xkid:configuration:observation:key:conex-y' : 'CONEXY',
    'xkid:configuration:observation:key:conex-ref-x' : 'CXREFX',
    'xkid:configuration:observation:key:conex-ref-y' : 'CXREFY',
    'xkid:configuration:observation:key:dpdcx' : 'DPDCX',
    'xkid:configuration:observation:key:dpdcy' : 'DPDCY',
    'xkid:configuration:observation:key:device-angle' : 'DEVANG',
    'xkid:configuration:observation:key:firmware-version' : 'FIRMV',
    'xkid:configuration:observation:key:filter-position' : 'FLTPOS',
    'xkid:configuration:observation:key:platescale' : 'PLTSCL',
    'xkid:configuration:observation:key:pixel-ref-x' : 'PREFX',
    'xkid:configuration:observation:key:pixel-ref-y' : 'PREFY',
    'xkid:configuration:observation:key:dark-file' : 'DARK',
    'xkid:configuration:observation:key:flat-file' : 'FLTCAL',
}

# Include all the MAGAOX KEYS
OBSLOG_RECORD_KEYS.update({v[0]:v[1] for v in MAGAOX_KEYS.values()})


def get_obslog_record():
    """
    Grab all the data needed for an observation (as ultimately specified in the mkidcore.metadata.XKID_KEY_INFO)
    from redis using the OBSLOG_RECORD_KEYS dictionary and build them into a astropy.io.fits.Header suitable for
    logging or fits - file building
    """
    try:
        kv_pairs = redis.read(list(OBSLOG_RECORD_KEYS.keys()), ts_value_only=True)
        fits_kv_pairs = [(OBSLOG_RECORD_KEYS[k], v) for k, v in kv_pairs]
    except RedisError:
        fits_kv_pairs=None
        getLogger(__name__).error('Failed to query redis for metadata. Most values will be defaults.')
    return metadata.build_header(metadata=fits_kv_pairs, use_simbad=False, KEY_INFO=metadata.XKID_KEY_INFO,
                                 DEFAULT_CARDSET=metadata.DEFAULT_XKID_CARDSET)


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


def parse_args():
    parser = argparse.ArgumentParser(description='XKID Observing Data Agent')
    parser.add_argument('--ip', default=7624, help='MagAO-X INDI port', destination='indi_port',
                        type=int, required=False)
    return parser.parse_args()


if __name__ == "__main__":

    # args = parse_args()
    util.setup_logging('observingAgent')
    redis.setup_redis(ts_keys=REDIS_TS_KEYS)

    indi = INDIClient('localhost', 7624)  # TODO add error handling and autorecovery
    indi.start()

    def indi2redis(element, changed):
        if changed:
            indikey = f'{element.property.device.name}.{element.property.name}.{element.name}'
            redis.store(MAGAOX_KEYS[indikey][0], element.value)

    for k in MAGAOX_KEYS:
        a, b, c = k.split('.')
        c.devices[a].properties[b].elements[c].add_watcher(indi2redis)

    gen2dashboard_yaml_to_redis(redis.read(DASHBOARD_YAML_KEY) or args.dashboard_yaml)

    default = dict(nRows=100, nCols=100, useWvl=False, nWvlBins=1, useEdgeBins=False, wvlStart=0.0, wvlStop=0.0)
    livecfg = redis.read(GUI_LIVE_IMAGE_DEFAUTS_KEY, decode_json=True) or default
    fitscfg = redis.read(FITS_IMAGE_DEFAULTS_KEY, decode_json=True) or default
    beammap = Beammap(redis.read(INSTRUMENT_BEAMMAP_FILE_KEY, error_missing=True))
    n_roaches = len(redis.read(GEN2_ROACHES_KEY, error_missing=True))  # TODO: (NS, 23 Feb 2023) Read in roaches in use from dashboard file to redis
    port = redis.read(GEN2_CAPTURE_PORT_KEY, error_missing=True)

    pm = Packetmaster(n_roaches, port, useWriter=True, sharedImageCfg={'live': livecfg, 'fits': fitscfg},
                      beammap=beammap, forwarding=False, recreate_images=True)
    fits_imagecube = pm.sharedImages['fits']

    limitless_integration = False
    fits_exp_time = None
    md_start = None
    request = {'type':'abort'}

    try:
        while True:
            if not limitless_integration:
                request = redis.listen(OBSERVING_REQUEST_CHANNEL, value_only=True, decode='json')
                if request['type'] == 'abort':
                    getLogger(__name__).debug(f'Request to stop while nothing in progress.')
                    pm.stopWriting()
                    continue

                limitless_integration = request['duration'] == 'inf'  # TODO: decide whether inf or 0 means infinite integration
                fits_exp_time = 60 if limitless_integration else request['duration']

                pm.startWriting(redis.read(BIN_FILE_DIR_KEY, decode_json=False))
                fits_imagecube.startIntegration(startTime=mkcu.next_utc_second(), integrationTime=fits_exp_time)
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
            image = fits.ImageHDU(data=im_data, header=merge_start_stop_headers(md_start, md_end))
            if limitless_integration:
                fits_imagecube.startIntegration(startTime=0, integrationTime=fits_exp_time)
                md_start = get_obslog_record()

            #TODO need to set these keys properly
            image.header['wavecal'] = fits_imagecube.wavecalID.decode('UTF-8', "backslashreplace")
            image.header['wmin'] = fits_imagecube.wvlStart
            image.header['wmax'] = fits_imagecube.wvlStop
            header_dict = dict(image.header)

            t = 'sum' if request['type'] in ('dwell', 'object') else request['type']
            fac = CalFactory(t, images=image, mask=redis.read(BAD_PIXEL_MASK_KEY),
                                flat=redis.read(ACTIVE_FLAT_FILE_KEY),
                                dark=redis.read(ACTIVE_DARK_FILE_KEY))

            if request['type'] == 'dark':
                fn = redis.read(DARK_FILE_TEMPLATE_KEY).format(**header_dict)
                name = os.path.splitext(os.path.basename(fn))[0]
                fac.generate(fname=fn, save=True, threaded=True, name=name, overwrite=True,
                             store_complete=(redis, ACTIVE_DARK_FILE_KEY))
            elif request['type'] == 'flat':
                fn = redis.read(FLAT_FILE_TEMPLATE_KEY).format(**header_dict)
                name = os.path.splitext(os.path.basename(fn))[0]
                fac.generate(fname=fn, save=True, name=name, overwrite=True, threaded=True,
                             store_complete=(redis, ACTIVE_FLAT_FILE_KEY))
            elif request['type'] in ('dwell', 'object'):
                fn = redis.read(SCI_FILE_TEMPLATE_KEY).format(**header_dict)
                fac.generate(fname=fn, name=request['name'], save=True, overwrite=True, threaded=True)

    except Exception:
        pm.quit()
        indi.stop()
