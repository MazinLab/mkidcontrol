#!/usr/bin/env python3
import queue
import threading
import time
import os
import argparse
from datetime import datetime
from mkidcontrol.mkidredis import RedisError
import mkidcontrol.mkidredis as redis
import mkidcontrol.util as util
from mkidcore.objects import Beammap  # must keep to have the yaml parser loaded
import typing
import calendar
from mkidcore import metadata
from mkidcore.config import load as load_yaml_config
from astropy.io import fits
from mkidcore.fits import CalFactory
from purepyindi2 import messages, client
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
DATA_DIR_KEY = 'paths:data-dir'
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
OBSERVING_EVENT_KEY = 'command:event:observing'

GEN2_REDIS_MAP = {'dashboard.max_count_rate': 'readout:count_rate_limit',
                  'beammap': BEAMMAP_FILE_KEY,
                  'roaches': GEN2_ROACHES_KEY,
                  'packetmaster.captureport': GEN2_CAPTURE_PORT_KEY,
                  'roaches.fpgpath': 'gen2:firmware-version'}

MAGAOX_KEYS = {
    'tcsi.telpos.am': ('tcs:airmass', 'AIRMASS', 'Airmass at start'),
    'tcsi.telpos.dec': ('tcs:dec', 'DEC', 'DEC of telescope pointing (+/-DD:MM:SS.SS)'),
    'tcsi.telpos.el': ('tcs:el', 'ALTITUDE', 'Elevation of telescope pointing'),
    'tcsi.telpos.epoch': ('tcs:epoch', 'EPOCH', 'Epoch of observation from MagAO-X'),
    'tcsi.telpos.ha': ('tcs:ha', 'HA', 'description'),
    'tcsi.telpos.ra': ('tcs:ra', 'RA', 'RA of telescope pointing (HH:MM:SS.SSS)'),
    'tcsi.telpos.rotoff': ('tcs:rotoff', 'ROTOFF', 'Telescope rotator offset'),
    'tcsi.teldata.az': ('tcs:az', 'AZIMUTH', 'Azimuth of telescope pointing'),
    'tcsi.teldata.dome_stat': ('tcs:dome-state', 'DOMESTAT', 'State of the dome at exposure start time'),
    'tcsi.teldata.guiding': ('tcs:guiding', 'GUIDING', 'Telescope guiding status'),
    'tcsi.teldata.pa': ('tcs:pa', 'POSANG', 'Position Angle'),
    'tcsi.teldata.slewing': ('tcs:slewing', 'SLEWING', 'Telescope slewing status'),
    'tcsi.teldata.tracking': ('tcs:tracking', 'TRACKING', 'Telescope tracking status'),
    'tcsi.teldata.zd': ('tcs:zd', 'ZD', 'Zenith distance at typical time'),
    'tcsi.teltime.sidereal_time': ('tcs:sidereal-time', 'SIDETIME', 'Sidereal time at typical time'),
    'tcsi.environment.dewpoint': ('tcs:dewpoint', 'DOM-DEW', 'Dewpoint'),
    'tcsi.environment.humidity': ('tcs:humidity', 'DOM-HUM', 'Humidity'),
    'tcsi.environment.temp-amb': ('tcs:temp-amb', 'DOM-TMPA', 'Ambient temperature'),
    'tcsi.environment.wind': ('tcs:wind', 'DOM-WND', 'Wind speed'),
    'tcsi.environment.winddir': ('tcs:winddir', 'DOM-WNDD', 'Wind direction'),
    'tcsi.catalog.object': ('tcs:catalog-object', 'CATOBJ', 'Catalog Object'),
    'tcsi.catdata.dec': ('tcs:catalog-dec', 'CATDEC', 'Catalog Dec.'),
    'tcsi.catdata.epoch': ('tcs:catalog-epoch', 'CATEPOCH', 'Catalog Epoch'),
    'tcsi.catdata.ra': ('tcs:catalog-ra', 'CATRA', 'CATRA', 'Catalog RA'),
    'tcsi.seeing.dimm_fwhm': ('tcs:seeing-dimm-fwhm', 'DIMMSEE', 'DIMM seeing (FWHM)'),
    'tcsi.seeing.dimm_fwhm_corr': ('tcs:seeing-dimm-fwhm-corr', 'DIMMSCOR', 'DIMM seeing correction'),
    'tcsi.seeing.mag2_el': ('tcs:seeing-el', 'SEEEL', 'Mag2 seeing elevation'),
    'tcsi.seeing.mag2_fwhm': ('tcs:seeing-fwhm', 'SEEING', 'Mag2 seeing (FWHM)'),
    'tcsi.seeing.mag2_fwhm_corr': ('tcs:seeing-fwhm-corr', 'SEECOR', 'Mag2 seeing correction'),
    'tcsi.seeing.mag2_time': ('tcs:seeing-time', 'SEETIM', 'Mag2 seeing time'),
}

START_FITS_KEYS = ('UNIXSTR', 'MJD-STR', 'UT-STR')  # TODO
MIDPOINT_FITS_KEYS = {'UNIXSTR': ('UNIXSTR', 'UNIXEND'),
                      'MJD-STR': ('MJD-STR', 'MJD-END'),
                      'UT-STR': ('UT-STR', 'UT-END')}  # TODO

OBSLOG_RECORD_KEYS = {
    # This should be a superset of mkidcore.metadata.XKID_KEY_INFO
    # Keys are redis keys, values are fits keys
    'status:temps:device-stage:temp': 'DET-TMP',
    'datasaver:beammap': 'BMAP',
    'paths:data-dir': 'CFGDIR',
    'datasaver:dark': 'DARK',
    'datasaver:flat': 'FLAT',
    'status:device:conex:position-x': 'CONEXX',
    'status:device:conex:position-y': 'CONEXY',
    'status:device:filterwheel:filter': 'FLTPOS',
    'status:device:focus:position-mm': 'FOCPOS',
    'status:device:laserflipperduino:flipper-position': 'FLPPOS',
    'status:device:heatswitch:position': 'HEATPOS',
    'status:device:laserflipperduino:laser-808': 'cal808',
    'status:device:laserflipperduino:laser-904': 'cal904',
    'status:device:laserflipperduino:laser-980': 'cal980',
    'status:device:laserflipperduino:laser-1120': 'cal1120',
    'status:device:laserflipperduino:laser-1310': 'cal1310',
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


def get_obslog_record(start, duration):
    """
    Grab all the data needed for an observation (as ultimately specified in the mkidcore.metadata.XKID_KEY_INFO)
    from redis using the OBSLOG_RECORD_KEYS dictionary and build them into a astropy.io.fits.Header suitable for
    logging or fits - file building
    """
    try:
        kv_pairs = redis.read(list(OBSLOG_RECORD_KEYS.keys()), ts_value_only=True, error_missing=False)
        fits_kv_pairs = {OBSLOG_RECORD_KEYS[k]: v for k, v in kv_pairs.items()}

    except RedisError:
        fits_kv_pairs = {}
        log.error('Failed to query redis for metadata. Most values will be defaults.')
    fits_kv_pairs['UNIXSTR'] = start
    fits_kv_pairs['UNIXEND'] = fits_kv_pairs['UNIXSTR'] + duration
    return metadata.build_header(metadata=fits_kv_pairs, use_simbad=False, KEY_INFO=metadata.XKID_KEY_INFO,
                                 DEFAULT_CARDSET=metadata.DEFAULT_XKID_CARDSET, unknown_keys='create')


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
        a, b = MIDPOINT_FITS_KEYS[k]
        header_stop[k] = (header_stop[a] + header_stop[b]) / 2
    return header_stop


def parse_args():
    parser = argparse.ArgumentParser(description='XKID Observing Data Agent')
    parser.add_argument('--indip', default=7624, help='MagAO-X INDI port', type=int, required=False)
    parser.add_argument('--test', default=False, help='Set testing REDIS values', action='store_true')
    return parser.parse_args()


class MagAOX_INDI2(threading.Thread):
    INTERESTING_DEVICES = ['tcsi', 'holoop', 'loloop', 'camwfs']

    def __init__(self, redis, *args, start=False, **kwargs):
        super(MagAOX_INDI2, self).__init__(*args, name='MagAO-X INDI Manager', **kwargs)
        self.daemon = True
        self.redis = redis
        self.log = log.getChild('magaox')
        if start:
            self.start()

    def indi2redis(self, message: messages.IndiMessage):
        if not isinstance(message, typing.get_args(messages.IndiDefSetMessage)):
            return
        device_name, prop_name = message.device, message.name
        update = {}
        for element_name, elem in message.elements():
            indikey = f'{device_name}.{prop_name}.{element_name}'
            if indikey not in MAGAOX_KEYS:
                continue
            # if metric_value in (None, float('inf'), float('-inf')):
            #     continue
            # ts = datetime.now().timestamp() if message.timestamp is None else message.timestamp.timestamp()
            update[MAGAOX_KEYS[indikey][0]] = elem.value
        if not update:
            return
        self.log.debug(update)
        self.redis.store(update)

    def run(self):
        while True:
            try:
                c = client.IndiClient()
                c.register_callback(self.indi2redis)
                c.connect()
                for device_name in self.INTERESTING_DEVICES:
                    c.get_properties(device_name)
                self.log.info("Listening for metrics")
                while True:
                    time.sleep(1)
            except Exception:
                self.log.exception("Restarting IndiClient on error...")
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
        DARK_FILE_TEMPLATE_KEY: 'xkid_dark_{UT-STR}_{UNIXSTR:.1f}.fits',
        FLAT_FILE_TEMPLATE_KEY: 'xkid_flat_{UT-STR}_{UNIXSTR:.1f}.fits',
        SCI_FILE_TEMPLATE_KEY: 'xkid_{UT-STR}_{UNIXSTR:.1f}.fits',
        'instrument:conex-ref-x': 0.0,
        'instrument:conex-ref-y': 0.0,
        'instrument:conex-dpdx': 0.0,
        'instrument:conex-dpdy': 0.0,
        'instrument:device-angle': 0.0,
        'instrument:platescale': 0.0,
        'instrument:pixel-ref-x': 1,
        'instrument:pixel-ref-y': 1,
    }
    redis.store(data)


def validate_request(x):
    try:
        assert 'type' in x, 'Missing observation type'
        assert x['type'] in ('stare', 'dwell', 'dark', 'flat', 'abort'), f'Invalid observation type {x["type"]}'
        if x['type'] != 'abort':
            assert 'name' in x, 'Request name missing'
            assert 'start' in x, 'start missing'
            assert x['start'] >= datetime.utcnow().timestamp() - 60, 'start must be no older than 60s from now'
            assert 'seq_n' in x, 'seq_n missing'
            assert isinstance(x['seq_n'], int) and x['seq_n'] >= 1, 'seq_n must be an int >= 1'
            assert 'seq_i' in x, 'seq_i missing'
            assert isinstance(x['seq_i'], int) and 0 <= x['seq_i'] < x['seq_n'], 'seq_i must be an int in [0,seq_n)'
            assert 'duration' in x, 'duration missing'
            assert x['duration'] >= 0, 'duration must not be negative'
    except AssertionError as e:
        raise e


def rotate_log(log, logs_dir):
    if not log.handlers or os.path.dirname(log.handlers[0].baseFilename) != logs_dir:
        handler = logging.FileHandler(os.path.join(logs_dir, f'obslog_{datetime.utcnow()}.json'), 'a')
        handler.setFormatter(logging.Formatter('%(message)s'))
        handler.setLevel('INFO')
        try:
            obs_log.removeHandler(log.handlers[0])
        except IndexError:
            pass
        log.addHandler(handler)


def _req_q_targ(redis, q):
    while True:
        try:
            for x in redis.listen(OBSERVING_REQUEST_CHANNEL, value_only=True, decode='json'):
                try:
                    validate_request(x)
                except AssertionError as e:
                    log.warning(f'Ignoring invalid observing request: {e}')
                    continue
                q.put(x)
        except RedisError as e:
            log.error(f'Error in command listener: {e}')


if __name__ == "__main__":
    util.setup_logging('observingAgent')
    redis.setup_redis(ts_keys=REDIS_TS_KEYS)

    args = parse_args()
    if args.test:
        pass
        # test_load_redis(redis)

    indi_thread = MagAOX_INDI2(redis, start=True)

    g2_cfg = gen2dashboard_yaml_to_redis(redis.read(DASHBOARD_YAML_KEY), redis)
    beammap = g2_cfg.beammap

    livecfg = redis.read(GUI_LIVE_IMAGE_DEFAULTS_KEY, decode_json=True)
    fitscfg = redis.read(FITS_IMAGE_DEFAULTS_KEY, decode_json=True)
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
    request_thread = threading.Thread(name='Command Listener', target=_req_q_targ, args=(redis, request_q))
    request_thread.daemon = True
    request_thread.start()
    FITS_FILE_TIME = 10

    last_request = {'name': '', 'state': 'stopped', 'seq_i': 0, 'seq_n': 0, 'start': 0, 'type': 'abort'}
    redis.store({OBSERVING_EVENT_KEY: last_request}, encode_json=True)
    try:
        while True:

            if not limitless_integration:
                request = request_q.get(block=True)
                if request['type'] == 'abort':
                    log.debug(f'Request to stop while nothing in progress.')
                    pm.stopWriting()
                    continue

                request['duration'] = int(request['duration'])
                last_request = request.copy()
                last_request['state'] = 'started'

                dur = 'infinite' if request['duration'] == 0 else f'{request["duration"]} s'
                log.info(f'Received request for {dur} {request["type"]} observation named '
                         f'{request["name"]}, {int(request["seq_i"])+1}/{request["seq_n"]}')
                limitless_integration = request['duration'] == 0
                fits_exp_time = FITS_FILE_TIME if limitless_integration else request['duration']

                bin_dir, fits_dir, logs_dir = update_paths()
                rotate_log(obs_log, logs_dir)

                pm.startWriting(bin_dir)
                x = datetime.utcnow()
                tics = [time.time()]
                fits_imagecube.startIntegration(startTime=calendar.timegm(datetime.utcnow().timetuple()) + 1,
                                                integrationTime=fits_exp_time)
                md_start = get_obslog_record(x.timestamp(), fits_exp_time)
                tics[-1] = time.time() - tics[-1]
                obs_log.info(json.dumps(dict(md_start)))
                redis.store({OBSERVING_EVENT_KEY: last_request}, encode_json=True)

            try:
                tics.append(time.time())
                request = request_q.get(timeout=fits_exp_time - .1)
            except queue.Empty:
                tics[-1] = time.time() - tics[-1]
            else:
                if request['type'] != 'abort':
                    log.warning(f'Ignoring observation request because one is already in progress')
                else:
                    pm.stopWriting()  # Stop writing photons, no need to touch the imagecube
                    # TODO write out a truncated fits?
                    limitless_integration = False
                    last_request['state'] = 'stopped'
                    redis.store({OBSERVING_EVENT_KEY: last_request}, encode_json=True)
                    log.info(f'Aborted observation of {request["type"]} "{request["name"]}",'
                             f'{int(request["seq_i"])+1}/{request["seq_n"]}.')
                    continue

            tics.append(time.time())
            im_data, start_t, expo_t = fits_imagecube.receiveImage(timeout=True, return_info=True)
            tics[-1] = time.time() - tics[-1]
            log.info(f'Start: {tics[0] * 1000:.0f} ms, Abort pause: {tics[1]:.2f} s, Receive pause: {tics[2]:.2f} s')
            md_end = get_obslog_record(datetime.utcnow().timestamp(), fits_exp_time)
            md_start['wavecal'] = md_end['wavecal'] = fits_imagecube.wavecalID  # .decode('UTF-8', "backslashreplace")
            md_start['wmin'] = md_end['wmin'] = fits_imagecube.wvlStart
            md_start['wmax'] = md_end['wmax'] = fits_imagecube.wvlStop
            image = fits.ImageHDU(data=im_data, header=merge_start_stop_headers(md_start, md_end))
            image.header['EXPTIME'] = fits_exp_time
            if limitless_integration:
                tics[0] = time.time()
                fits_imagecube.startIntegration(startTime=0, integrationTime=fits_exp_time)
                tics[0] = time.time() - tics[0]
                md_start = get_obslog_record(datetime.utcnow().timestamp(), fits_exp_time)
            else:
                last_request['state'] = 'stopped'
                redis.store({OBSERVING_EVENT_KEY: last_request}, encode_json=True)
                log.info(f'Observation of {request["type"]} "{request["name"]}", '
                         f'{int(request["seq_i"])+1}/{request["seq_n"]} complete')

            header_dict = dict(image.header)

            t = 'sum' if request['type'] in ('dwell', 'stare') else request['type']
            fac = CalFactory(t, images=image, mask=beammap.failmask,
                             flat=redis.read(ACTIVE_FLAT_FILE_KEY),
                             dark=redis.read(ACTIVE_DARK_FILE_KEY))

            if request['type'] == 'dark':
                fn = os.path.join(fits_dir, redis.read(DARK_FILE_TEMPLATE_KEY).format(**header_dict))
                name = os.path.splitext(os.path.basename(fn))[0]
                fac.generate(fname=fn, save=True, threaded=True, name=name, overwrite=True,
                             complete_callback=lambda x: redis.store({ACTIVE_DARK_FILE_KEY: x}))
            elif request['type'] == 'flat':
                fn = os.path.join(fits_dir, redis.read(FLAT_FILE_TEMPLATE_KEY).format(**header_dict))
                name = os.path.splitext(os.path.basename(fn))[0]
                fac.generate(fname=fn, save=True, name=name, overwrite=True, threaded=True,
                             complete_callback=lambda x: redis.store({ACTIVE_FLAT_FILE_KEY: x}))
            elif request['type'] in ('dwell', 'stare'):
                fn = os.path.join(fits_dir, redis.read(SCI_FILE_TEMPLATE_KEY).format(**header_dict))
                fac.generate(fname=fn, name=request['name'], save=True, overwrite=True, threaded=True,
                             complete_callback=lambda x: redis.store({LAST_SCI_FILE_KEY: x}))

            if not limitless_integration:
                tics = []

    except Exception as e:
        log.critical(f'Fatal Error: {e}')
        pm.quit()
        raise
