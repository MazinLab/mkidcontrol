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
from git import Repo
from pkg_resources import resource_filename

repo = Repo(os.path.dirname(resource_filename('mkidcontrol', '')))
GIT_HASH = commit_hash = repo.git.rev_parse("HEAD")

log = logging.getLogger('observingAgent')

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

START_FITS_KEYS = ('UNIXSTR', 'MJD-STR', 'UT-STR')
MIDPOINT_FITS_KEYS = {'UNIXSTR': ('UNIXSTR', 'UNIXEND')}

MAGAOX_REDIS2INDI = {x.redis_key: x.indi_value for x in metadata.XKID_KEY_INFO.values() if x.indi_value}
MAGAOX_INDI2REDIS = {v:k for k,v in MAGAOX_REDIS2INDI.items()}
MAGAOX_FILTER_PROPS = ('stagebs.presetName', 'fwpupil.filterName', 'fwfpm.filterName', 'fwlyot.filterName',
                       'stagepiaa1.presetName', 'stagepiaa2.presetName')
for k in MAGAOX_FILTER_PROPS:
    MAGAOX_INDI2REDIS[k] = MAGAOX_INDI2REDIS[f'{k}.XXX']


def get_obslog_record(start=0.0, stop=0.0, duration=0.0, keys=None):
    """
    Grab all the data needed for an observation (as ultimately specified in the mkidcore.metadata.XKID_KEY_INFO)
    from redis using the OBSLOG_RECORD_KEYS dictionary and build them into a astropy.io.fits.Header suitable for
    logging or fits - file building
    """
    kv_pairs = {}
    try:
        redis_keys = [x.redis_key for x in metadata.XKID_KEY_INFO.values() if x.redis_key!='.']
        kv_pairs = redis.read(redis_keys, ts_value_only=True, error_missing=False)
    except RedisError:
        log.error('Failed to query redis for metadata. Most values will be defaults.')

    fits_kv_pairs = {metadata.XKID_REDIS_TO_FITS[k]: v for k, v in kv_pairs.items()}

    if keys:
        for k, v in keys.items():
            fits_kv_pairs[k] = v

    fits_kv_pairs['UNIXSTR'] = start
    fits_kv_pairs['UNIXEND'] = stop
    fits_kv_pairs['EXPTIME'] = duration
    return metadata.build_header(metadata=fits_kv_pairs, use_simbad=False, KEY_INFO=metadata.XKID_KEY_INFO,
                                 DEFAULT_CARDSET=metadata.DEFAULT_XKID_CARDSET,
                                 TIME_KEYS=metadata.XKID_TIME_KEYS,
                                 TIME_KEY_BUILDER=metadata.xkid_time_builder,
                                 unknown_keys='error')


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
    INTERESTING_DEVICES = [x.partition('.')[0] for x in MAGAOX_REDIS2INDI.values()]
    INTERESTING_PROPERTIES = [x.rpartition('.')[0] for x in MAGAOX_REDIS2INDI.values()]

    def __init__(self, redis, *args, start=False, **kwargs):
        super(MagAOX_INDI2, self).__init__(*args, name='MagAO-X INDI Manager', **kwargs)
        self.daemon = True
        self.redis = redis
        self.log = log.getChild('magaox')
        self.client = None
        if start:
            self.start()

    def indi2redis(self, message: messages.IndiMessage):
        if not isinstance(message, typing.get_args(messages.IndiDefSetMessage)):
            return
        device_name, prop_name = message.device, message.name
        update = {}  # redis key: value

        for element_name, elem in message.elements():
            indikey = f'{device_name}.{prop_name}'
            if indikey not in self.INTERESTING_PROPERTIES:
                continue
            elif indikey in MAGAOX_FILTER_PROPS:
                try:
                    for a in self.client[indikey]:
                        if self.client[indikey][a].value.lower() == 'on':
                            update[MAGAOX_INDI2REDIS[indikey]] = a
                except KeyError as e:
                    self.log.error(f'Unable to fetch {MAGAOX_INDI2REDIS[indikey]} from MagAO-X due to {e}')
            else:
                indikey += f'.{element_name}'
                if indikey not in MAGAOX_INDI2REDIS.values():
                    continue
                # if metric_value in (None, float('inf'), float('-inf')):
                #     continue
                # ts = datetime.now().timestamp() if message.timestamp is None else message.timestamp.timestamp()
                update[MAGAOX_INDI2REDIS[indikey]] = elem.value
        if not update:
            return
        self.log.debug(update)
        self.redis.store(update)

    def run(self):

        while True:
            try:
                self.client = client.IndiClient()
                self.client.register_callback(self.indi2redis)
                self.client.connect()
                for device_name in self.INTERESTING_DEVICES:
                    self.client.get_properties(device_name)
                self.log.info("Listening for metrics")
                while True:
                    time.sleep(1)
            except Exception:
                self.log.exception("Restarting IndiClient on error...")
                time.sleep(1)


def update_paths():
    d = redis.read([DATA_DIR_KEY, 'paths:fits-folder-name', 'paths:logs-folder-name', 'paths:bin-folder-name'])
    data_dir = d['paths:data-dir']
    fits_dir = os.path.join(data_dir, d['paths:fits-folder-name'])
    logs_dir = os.path.join(data_dir, d['paths:logs-folder-name'])
    bin_dir = os.path.join(data_dir, d['paths:bin-folder-name'])
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


def fetch_request(request_q, block=True, timeout=None):
    req = request_q.get(block=block, timeout=timeout)
    if req['type'] == 'abort':
        log.info(f'Received abort request')
        return None, None, None, True
    req['duration'] = int(req['duration'])
    inf = req['duration'] == 0
    dur = 'infinite' if limitless else f'{req["duration"]} s'
    log.info(f'Received request for {dur} {req["type"]} observation named '
             f'{req["name"]}, {int(req["seq_i"]) + 1}/{req["seq_n"]}')
    head = {'OBJECT': req['name'], 'E_GITHSH': GIT_HASH, 'DATA-TYP': req['type']}
    return req, head, inf, False


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

    limitless = False
    # fits_exp_time = None
    # md_start = None
    # request = {'type': 'abort'}

    obs_log = logging.getLogger('obs_log')
    obs_log.propagate = False
    obs_log.setLevel('INFO')
    bin_dir, fits_dir, logs_dir = update_paths()

    request_q = Queue()
    request_thread = threading.Thread(name='Command Listener', target=_req_q_targ, args=(redis, request_q))
    request_thread.daemon = True
    request_thread.start()
    FITS_FILE_TIME = 10

    redis.store({OBSERVING_EVENT_KEY: {'name': '', 'state': 'stopped', 'seq_i': 0,
                                       'seq_n': 0, 'start': 0, 'type': 'stare'}}, encode_json=True)
    try:
        while True:

            if not limitless:
                request, header_info, limitless, abort = fetch_request(request_q, block=True)

                if abort:
                    log.debug(f'Request to stop while nothing in progress.')
                    pm.stopWriting()
                    continue

                fits_exp_time = FITS_FILE_TIME if limitless else request['duration']

                bin_dir, fits_dir, logs_dir = update_paths()
                rotate_log(obs_log, logs_dir)

                pm.startWriting(bin_dir)
                start_time = calendar.timegm(datetime.utcnow().timetuple()) + 1
                fits_imagecube.startIntegration(startTime=start_time, integrationTime=fits_exp_time)
                md_start = get_obslog_record(start=start_time, duration=fits_exp_time, keys=header_info)
                obs_log.info(json.dumps(dict(md_start)))
                request['state'] = 'started'
                redis.store({OBSERVING_EVENT_KEY: request}, encode_json=True)

            try:
                _, _, _, abort = fetch_request(request_q, timeout=fits_exp_time - .05)
            except queue.Empty:
                pass
            else:
                if abort:
                    pm.stopWriting()  # Stop writing photons, no need to touch the imagecube
                    limitless = False
                    request['state'] = 'stopped'
                    redis.store({OBSERVING_EVENT_KEY: request}, encode_json=True)
                    log.info(f'Aborted observation of {request["type"]} "{request["name"]}",'
                             f'{int(request["seq_i"]) + 1}/{request["seq_n"]}.')
                    continue
                else:
                    log.warning(f'Ignored observation request because one is already in progress')

            im_data, start_t, expo_t = fits_imagecube.receiveImage(timeout=True, return_info=True)
            md_end = get_obslog_record(start=md_start['UNIXSTR'], stop=datetime.utcnow().timestamp(),
                                       duration=fits_exp_time, keys=header_info)

            # md_start['FRATE'] = md_end['FRATE']=
            md_start['wavecal'] = md_end['wavecal'] = fits_imagecube.wavecalID  # .decode('UTF-8', "backslashreplace")
            md_start['wmin'] = md_end['wmin'] = fits_imagecube.wvlStart
            md_start['wmax'] = md_end['wmax'] = fits_imagecube.wvlStop
            header = merge_start_stop_headers(md_start, md_end)
            image = fits.ImageHDU(data=im_data, header=header)
            obs_log.info(json.dumps(dict(header)))

            if limitless:
                fits_imagecube.startIntegration(startTime=0, integrationTime=fits_exp_time)
                md_start = get_obslog_record(start=datetime.utcnow().timestamp(), keys=header_info)
            else:
                request['state'] = 'stopped'
                redis.store({OBSERVING_EVENT_KEY: request}, encode_json=True)
                log.info(f'Observation of {request["type"]} "{request["name"]}", '
                         f'{int(request["seq_i"]) + 1}/{request["seq_n"]} complete')

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

    except Exception as e:
        log.critical(f'Fatal Error: {e}')
        pm.quit()
        raise
