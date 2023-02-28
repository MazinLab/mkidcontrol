#!/usr/bin/env python3
import threading
import time
from logging import getLogger
import os
import argparse
from datetime import datetime
from mkidcontrol.mkidredis import RedisError
import mkidcontrol.mkidredis as redis
import mkidcontrol.util as util
from mkidcore.objects import Beammap  # must keep to have the yaml parser loaded
from mkidcore import metadata
from mkidcore import utils as mkcu
from mkidcore.config import load as load_yaml_config
from astropy.io import fits
from mkidcore.fits import CalFactory
from purepyindi.client import INDIClient, ConnectionStatus
from mkidcontrol.packetmaster3.packetmaster import Packetmaster
from mkidcontrol.config import REDIS_TS_KEYS
import logging
import json
from queue import Queue

metadata.TIME_KEYS = ('MJD-END', 'MJD-STR', 'UT-END', 'UT-STR')
metadata._time_key_builder = metadata._xkid_time_header

log = logging.getLogger('observingAgent')

TS_KEYS = REDIS_TS_KEYS

DASHBOARD_YAML_KEY = 'gen2:dashboard-yaml'
GEN2_ROACHES_KEY = 'gen2:roaches'

GEN2_CAPTURE_PORT_KEY = 'datasaver:capture-port'
DATA_DIR_KEY = 'paths:bin-dir'
ACTIVE_DARK_FILE_KEY = 'datasaver:dark'  # a FQP to the active dark fits image, if any
ACTIVE_FLAT_FILE_KEY = 'datasaver:flat'  # a FQP to the active flat fits image, if any
LAST_SCI_FILE_KEY = 'datasaver:sci'  # a FQP to the active flat fits image, if any
SCI_FILE_TEMPLATE_KEY = 'datasaver:sci-template'  # a template filename that will be formatted with metadata
DARK_FILE_TEMPLATE_KEY = 'datasaver:dark-template'  # a template filename that will be formatted with metadata
FLAT_FILE_TEMPLATE_KEY = 'datasaver:flat-template'  # a template filename that will be formatted with metadata
FITS_IMAGE_DEFAULTS_KEY = 'datasaver:fits_image_config'
BEAMMAP_FILE_KEY = 'datasaver:beammap'

GUI_LIVE_IMAGE_DEFAULTS_KEY = 'gui:live_image_config'

OBSERVING_REQUEST_CHANNEL = 'command:observation-request'

GEN2_REDIS_MAP = {'dashboard.max_count_rate': 'readout:count_rate_limit',
                  'beammap': BEAMMAP_FILE_KEY,
                  'roaches': GEN2_ROACHES_KEY,
                  'packetmaster.captureport': GEN2_CAPTURE_PORT_KEY,
                  'roaches.fpgpath': 'instrument:firmware-version'}

MAGAOX_KEYS = {
    'tcsi.telpos.am': ('tcs:airmass', 'AIRMASS', 'Airmass at start'),
    'tcsi.telpos.dec': ('tcs:dec', 'DEC', 'DEC of telescope pointing (+/-DD:MM:SS.SS)'),
    'tcsi.telpos.el': ('tcs:el', 'ALTITUDE', 'Elevation of telescope pointing'),
    'tcsi.telpos.epoch': ('tcs:epoch', 'EPOCH', 'Epoch of observation from MagAO-X'),
    'tcsi.telpos.ha': ('tcs:ha', 'HA', 'description'),  # TODO: Not sure?
    'tcsi.telpos.ra': ('tcs:ra', 'RA', 'RA of telescope pointing (HH:MM:SS.SSS)'),
    'tcsi.telpos.rotoff': ('tcs:rotoff', 'ROT_STAT', 'Telescope rotator on/off'),
    'tcsi.teldata.az': ('tcs:az', 'AZIMUTH', 'Azimuth of telescope pointing'),
    'tcsi.teldata.dome_stat': ('tcs:dome-state', 'DOM-STAT', 'State of the dome at exposure start time'),
    'tcsi.teldata.guiding': ('tcs:guiding', 'fitskey', 'Telescope guiding status'),
    'tcsi.teldata.pa': ('tcs:pa', 'fitskey', 'Position Angle'),  # TODO: Position angle of what?
    'tcsi.teldata.slewing': ('tcs:slewing', 'SLEWING', 'Telescope slewing status'),
    'tcsi.teldata.tracking': ('tcs:tracking', 'TRACKING', 'Telescope tracking status'),
    'tcsi.teldata.zd': ('tcs:zd', 'ZD', 'Zenith distance at typical time'),
    'tcsi.teltime.sidereal_time': ('tcs:sidereal-time', 'SID-TIME', 'Sidereal time at typical time'),
    # TODO: Sidereal time at start and end?
    'tcsi.environment.dewpoint': ('tcs:dewpoint', 'DOM-DEW', 'Dewpoint'),
    'tcsi.environment.humidity': ('tcs:humidity', 'DOM-HUM', 'Humidity'),
    'tcsi.environment.temp-amb': ('tcs:temp-amb', 'DOM-TMPA', 'Ambient temperature'),
    'tcsi.environment.wind': ('tcs:wind', 'DOM-WND', 'Wind speed'),
    'tcsi.environment.winddir': ('tcs:winddir', 'DOM-WNDD', 'Wind direction'),
    'tcsi.catalog.object': ('tcs:catalog-object', 'OBJECT', 'Object'),
    'tcsi.catdata.dec': ('tcs:catalog-dec', 'fits', 'desc'),  # TODO: Not sure
    'tcsi.catdata.epoch': ('tcs:catalog-epoch', 'fits', 'desc'),  # TODO: Not sure
    'tcsi.catdata.ra': ('tcs:catalog-ra', 'fits', 'desc'),  # TODO: Not sure
    'tcsi.catdata.rotoff': ('tcs:catalog-rotoff', 'fits', 'desc'),  # TODO: Not sure
    'tcsi.seeing.dimm_fwhm': ('tcs:seeing-dimm-fwhm', 'DIMM-SEE', 'DIMM seeing (FWHM)'),
    'tcsi.seeing.dimm_fwhm_corr': ('tcs:seeing-dimm-fwhm-corr', 'DIMM-COR', 'desc'),  # TODO: Not sure
    'tcsi.seeing.mag2_el': ('tcs:seeing-el', 'MAG2-EL', 'Mag2 elevation'),
    'tcsi.seeing.mag2_fwhm': ('tcs:seeing-fwhm', 'MAG2-SEE', 'Mag2 seeing (FWHM)'),
    'tcsi.seeing.mag2_fwhm_corr': ('tcs:seeing--fwhm-corr', 'MAG2-COR', 'desc'),  # TODO: Not sure
    'tcsi.seeing.mag2_time': ('tcs:seeing-time', 'MAG2-TIM', 'desc'),  # TODO: Not sure
}

START_FITS_KEYS = tuple()  # TODO
MIDPOINT_FITS_KEYS = tuple()  # TODO

OBSLOG_RECORD_KEYS = {
    # This should be a superset of mkidcore.metadata.XKID_KEY_INFO
    # Keys are redis keys, values are fits keys
    'status:temps:device:temp': 'DET-TMP',
    'datasaver:beammap': 'BMAP',
    'paths:data-dir': 'CFGDIR',
    'datasaver:dark': 'DARK',
    'datasaver:flat': 'FLAT',
    'status:device:conex:position:x': 'CONEXX',
    'status:device:conex:position:y': 'CONEXY',
    'status:filterwheel:position': 'FLTPOS',
    'laserflipperduino:flipper:position': 'FLPPOS',
    'laserflipperduino:laserbox:808:power': 'cal808',
    'laserflipperduino:laserbox:904:power': 'cal904',
    'laserflipperduino:laserbox:980:power': 'cal980',
    'laserflipperduino:laserbox:1120:power': 'cal1120',
    'laserflipperduino:laserbox:1310:power': 'cal1310',
    'instrument:platescale': 'PLTSCL',
    'instrument:pixel-ref-x': 'PREFX',
    'instrument:pixel-ref-y': 'PREFY',
    'instrument:conex-ref-x': 'CXREFX',
    'instrument:conex-ref-y': 'CXREFY',
    'instrument:conex-dpdx': 'CDPDX',
    'instrument:conex-dpdy': 'CDPDY',
    'instrument:device-angle': 'DEVANG',
    'instrument:firmware-version': 'FIRMV'
}

# Include all the MAGAOX KEYS
OBSLOG_RECORD_KEYS.update({v[0]: v[1] for v in MAGAOX_KEYS.values()})


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
        fits_kv_pairs = None
        getLogger(__name__).error('Failed to query redis for metadata. Most values will be defaults.')
    return metadata.build_header(metadata=fits_kv_pairs, use_simbad=False, KEY_INFO=metadata.XKID_KEY_INFO,
                                 DEFAULT_CARDSET=metadata.DEFAULT_XKID_CARDSET)


def gen2dashboard_yaml_to_redis(yaml, redis):
    """
    Loads a gen2 dashboard yaml config and put all the required things into the redis database at their appropriate
    keys
    """
    c = load_yaml_config(yaml)

    def cfg_tuple(map_item):
        yaml_key, redis_key = map_item
        v = c.get(yaml_key)
        if 'beammap' in yaml_key.lower():
            v = v.file
        try:
            v = json.dumps(v.todict())
        except AttributeError:
            pass
        return redis_key, v

    redis.store([cfg_tuple(x) for x in GEN2_REDIS_MAP.items()])
    return c


def merge_start_stop_headers(header_start, header_stop):
    """Build a final observation header out of the start and stop headers"""
    for k in START_FITS_KEYS:
        header_stop[k] = header_start[k]
    for k in MIDPOINT_FITS_KEYS:
        header_stop[k] = (header_start[k] + header_stop[k]) / 2
    return header_stop


def parse_args():
    parser = argparse.ArgumentParser(description='XKID Observing Data Agent')
    parser.add_argument('--indip', default=7624, help='MagAO-X INDI port', type=int, required=False)
    parser.add_argument('--test', default=False, help='Set testing REDIS values', action='store_true')
    return parser.parse_args()


class MagAOX_INDI(threading.Thread):
    def __init__(self, redis, *args, start=False, **kwargs):
        super(MagAOX_INDI, self).__init__(name='MagAO-X INDI Manager')
        self.daemon = True
        self.redis = redis
        if start:
            self.start()

    def run(self):
        def indi2redis(element, changed):
            if changed:
                indikey = f'{element.property.device.name}.{element.property.name}.{element.name}'
                log.getChild('magaox').debug(MAGAOX_KEYS[indikey][0], element.value)
                self.redis.store(MAGAOX_KEYS[indikey][0], element.value)

        from collections import defaultdict
        keys_by_device = defaultdict(list)
        for k in MAGAOX_KEYS:
            d, p, e = k.split('.')
            keys_by_device[d].append((p, e))

        indi = INDIClient('localhost', 7624)  # TODO add error handling and autorecovery
        unwatched = set(keys_by_device.keys())
        first_start = True

        while True:

            if indi.status == ConnectionStatus.ERROR or first_start:
                log.info("Starting MagAO-X connection")
                try:
                    indi.start()
                    first_start = False
                except Exception:
                    log.error(f"Failed to start, connection status: {indi.status}")
                # unwatched = set(keys_by_device.keys())  #TODO

            while unwatched and indi.status == ConnectionStatus.CONNECTED:
                for d in list(unwatched):
                    try:
                        dev = indi.devices[d]
                    except Exception:
                        log.getChild('magaox').debug(f'Device {d} not available')
                        continue

                    try:
                        for p, e in keys_by_device[d]:
                            dev.properties[p].elements[e].add_watcher(indi2redis)
                    except KeyError as e:
                        log.getChild('magaox').debug(f'Device {d} missing {p}, {e}')
                    else:
                        unwatched.discard(d)

            time.sleep(1)


def update_paths():
    data_dir = redis.read(DATA_DIR_KEY, decode_json=False)
    fits_dir = os.path.join(data_dir, 'fits')
    logs_dir = os.path.join(data_dir, 'logs')
    bin_dir = os.path.join(data_dir, 'bin')
    os.makedirs(fits_dir, exist_ok=True)
    os.makedirs(logs_dir, exist_ok=True)
    os.makedirs(bin_dir, exist_ok=True)
    return bin_dir, fits_dir, logs_dir


DEFAULT_PM_IMAGE_CFG = dict(nRows=125, nCols=80, useWvl=False, nWvlBins=1, useEdgeBins=False, wvlStart=0.0, wvlStop=0.0)


def test_load_redis(redis):
    data = {
        DASHBOARD_YAML_KEY: '/home/kids/src/mkidcontrol/mkidreadout/mkidreadout/config/dashboard.yml',
        GUI_LIVE_IMAGE_DEFAULTS_KEY: json.dumps(DEFAULT_PM_IMAGE_CFG),
        FITS_IMAGE_DEFAULTS_KEY: json.dumps(DEFAULT_PM_IMAGE_CFG),
        DATA_DIR_KEY: '/home/kids/testing/binfiles',
        ACTIVE_FLAT_FILE_KEY: '',
        ACTIVE_DARK_FILE_KEY: '',
        LAST_SCI_FILE_KEY: '',
        DARK_FILE_TEMPLATE_KEY: 'xkid_dark_{UT-STR}_{UNIXTIME}.fits',
        FLAT_FILE_TEMPLATE_KEY: 'xkid_flat_{UT-STR}_{UNIXTIME}.fits',
        SCI_FILE_TEMPLATE_KEY: 'xkid_{UT-STR}_{UNIXTIME}.fits'
    }
    redis.store(data)


def validate_request(x):
    try:
        assert 'type' in x and x['type'] in ('stare','dwell','dark','flat','abort'), 'Missing/Invalid observation type'
        if x['type'] !='abort':
            assert 'name' in x, 'Request name missing'
            assert 'start' in x, 'start missing'
            assert 'seq_i' in x, 'seq_i missing'
            assert 'seq_n' in x, 'seq_n missing'
            assert 'duration' in x, 'duration missing'
    except AssertionError as e:
        raise e




if __name__ == "__main__":

    util.setup_logging('observingAgent')
    redis.setup_redis(ts_keys=REDIS_TS_KEYS)

    args = parse_args()
    if args.test:
        test_load_redis(redis)

    indi_thread = MagAOX_INDI(redis, start=False)

    g2_cfg = gen2dashboard_yaml_to_redis(redis.read(DASHBOARD_YAML_KEY), redis)
    beammap = g2_cfg.beammap

    livecfg = redis.read(GUI_LIVE_IMAGE_DEFAULTS_KEY, decode_json=True) or DEFAULT_PM_IMAGE_CFG
    fitscfg = redis.read(FITS_IMAGE_DEFAULTS_KEY, decode_json=True) or DEFAULT_PM_IMAGE_CFG
    n_roaches = len(redis.read(GEN2_ROACHES_KEY, error_missing=True, decode_json=True)['in_use'])
    port = redis.read(GEN2_CAPTURE_PORT_KEY, error_missing=True, decode_json=True)

    pm = Packetmaster(n_roaches, port, useWriter=True, sharedImageCfg={'live': livecfg, 'fits': fitscfg},
                      beammap=beammap, recreate_images=True)
    fits_imagecube = pm.sharedImages['fits']

    limitless_integration = False
    fits_exp_time = None
    md_start = None
    request = {'type': 'abort'}

    obs_log = logging.getLogger('obs_log')
    obs_log.propagate = False
    obs_log.setLevel('INFO')
    bin_dir, fits_dir, logs_dir = update_paths()

    request_q = Queue()

    def _req_q_targ():
        while True:
            try:
                for x in redis.listen(OBSERVING_REQUEST_CHANNEL, value_only=True, decode='json'):
                    try:
                        validate_request(x)
                    except AssertionError as e:
                        log.warning(f'Ignoring invalid observing request: {e}')
                        continue
                    request_q.put(x)
            except RedisError as e:
                log.error(f'Error in command listener: {e}')


    request_thread = threading.Thread(name='Command Listener', target=_req_q_targ)
    request_thread.daemon = True
    request_thread.start()
    try:
        while True:
            if not limitless_integration:
                request = request_q.get(block=True)

                if request['type'] == 'abort':
                    getLogger(__name__).debug(f'Request to stop while nothing in progress.')
                    pm.stopWriting()
                    continue

                limitless_integration = request['duration'] == 0
                fits_exp_time = 60 if limitless_integration else request['duration']

                bin_dir, fits_dir, logs_dir = update_paths()
                if not obs_log.handlers or os.path.dirname(obs_log.handlers[0].baseFilename) != logs_dir:
                    handler = logging.FileHandler(os.path.join(logs_dir, f'obslog_{datetime.utcnow()}.json'), 'a')
                    handler.setFormatter(logging.Formatter('%(message)s'))
                    handler.setLevel('INFO')
                    try:
                        obs_log.removeHandler(obs_log.handlers[0])
                    except IndexError:
                        pass
                    obs_log.addHandler(handler)

                pm.startWriting(bin_dir)
                fits_imagecube.startIntegration(startTime=mkcu.next_utc_second(), integrationTime=fits_exp_time)
                md_start = get_obslog_record()
                obs_log.info(json.dumps(md_start))

            try:
                request = request_q.get(timeout=fits_exp_time - .1)
            except TimeoutError:
                pass

            else:
                if request['type'] != 'abort':
                    getLogger(__name__).warning(f'Ignoring observation request because one is already in progress')
                else:
                    # Stop writing photons, no need to touch the imagecube
                    pm.stopWriting()
                    limitless_integration = False
                    continue

            im_data, start_t, expo_t = fits_imagecube.receiveImage(timeout=False, return_info=True)
            md_end = get_obslog_record()
            image = fits.ImageHDU(data=im_data, header=merge_start_stop_headers(md_start, md_end))
            if limitless_integration:
                fits_imagecube.startIntegration(startTime=0, integrationTime=fits_exp_time)
                md_start = get_obslog_record()

            # TODO need to set these keys properly
            image.header['wavecal'] = fits_imagecube.wavecalID.decode('UTF-8', "backslashreplace")
            image.header['wmin'] = fits_imagecube.wvlStart
            image.header['wmax'] = fits_imagecube.wvlStop
            header_dict = dict(image.header)

            t = 'sum' if request['type'] in ('dwell', 'object') else request['type']
            fac = CalFactory(t, images=image, mask=beammap.failmask,
                             flat=redis.read(ACTIVE_FLAT_FILE_KEY),
                             dark=redis.read(ACTIVE_DARK_FILE_KEY))

            if request['type'] == 'dark':
                fn = os.path.join(fits_dir, redis.read(DARK_FILE_TEMPLATE_KEY).format(**header_dict))
                name = os.path.splitext(os.path.basename(fn))[0]
                fac.generate(fname=fn, save=True, threaded=True, name=name, overwrite=True,
                             complete_callback=lambda x: redis.store(ACTIVE_DARK_FILE_KEY, x))
            elif request['type'] == 'flat':
                fn = os.path.join(fits_dir, redis.read(FLAT_FILE_TEMPLATE_KEY).format(**header_dict))
                name = os.path.splitext(os.path.basename(fn))[0]
                fac.generate(fname=fn, save=True, name=name, overwrite=True, threaded=True,
                             complete_callback=lambda x: redis.store(ACTIVE_FLAT_FILE_KEY, x))
            elif request['type'] in ('dwell', 'object'):
                fn = os.path.join(fits_dir, redis.read(SCI_FILE_TEMPLATE_KEY).format(**header_dict))
                fac.generate(fname=fn, name=request['name'], save=True, overwrite=True, threaded=True,
                             complete_callback=lambda x: redis.store(LAST_SCI_FILE_KEY, x))

    except Exception:
        pm.quit()
