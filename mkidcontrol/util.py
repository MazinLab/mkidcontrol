import logging.config
import numpy as np
import scipy.stats
import pkg_resources
import os
import yaml
from glob import glob
import subprocess
from logging import getLogger
import psutil


def setup_logging(name):
    path = pkg_resources.resource_filename('mkidcontrol', '../configuration/logging.yml')
    if os.path.exists(path):
        with open(path, 'rt') as f:
            config = yaml.safe_load(f.read())

    # postprocess loggers dict
    # keys are program names values are either
    #   1) a level for the log of the program
    #   2) a dict of log names and levels
    #   3) a dict of log names and dicts describing how to configure the corresponding Logger instance.
    #  See https://docs.python.org/3/library/logging.config.html#logging-config-dictschema
    # The configuring dict is searched for the following keys:
    #     level (optional). The level of the logger.
    #     propagate (optional). The propagation setting of the logger.
    #     filters (optional). A list of ids of the filters for this logger.
    #     handlers (optional). A list of ids of the handlers for this logger.
    cfg = config['loggers'][name]  # extract one we care about
    if isinstance(cfg, str):
        config['loggers'] = {name: {'level': cfg.upper()}}
    else:
        loggers = {}
        for k, v in cfg.items():
            loggers[k] = {'level': v.upper()} if isinstance(v, str) else v
        config['loggers'] = loggers

    logging.config.dictConfig(config)
    return logging.getLogger(name)

def gkern(kernlen=51, nsig=3):
    """Returns a 2D Gaussian kernel."""
    global _cache_gkern
    try:
        return _cache_gkern[(kernlen, nsig)]
    except KeyError:
        pass
    except NameError:
        _cache_gkern = {}
    x = np.linspace(-nsig, nsig, kernlen + 1)
    kern1d = np.diff(scipy.stats.norm.cdf(x))
    _cache_gkern[(kernlen, nsig)] = k = kern1d / kern1d.max()
    return k.copy()


def video_to_frames(file):
    import cv2
    vidcap = cv2.VideoCapture(file)
    success, image = vidcap.read()
    frames = []
    while success:
        frames.append(image)
        success, image = vidcap.read()
    vidcap.release()


class GracefulKiller:
    kill_now = False

    def __init__(self):
        import signal
        signal.signal(signal.SIGINT, self.exit_gracefully)
        signal.signal(signal.SIGTERM, self.exit_gracefully)

    def exit_gracefully(self, *args):
        self.kill_now = True


SERVICE_DESCRIPTIONS = {
    # TODO
    'fadecandy.service': "OpenPixelControl server, receives LED commands and controls the fadecandy",
    'cloud-web.service': "Cloud website (gunicorn)",
    'cloud-web.socket': 'Web connection listener, starts the webserver as needed',
    'cloud-player.service': 'Audio effect engine',
    'cloud-lamp.service': 'Lightshow effect engine',
    'cloud-speaker.service': 'Speaker controller, keeps speaker connected',
    'cloud-rqwork.service': 'Redis queue service, performs asynchronous jobs',
    'cloud-rqsched.service': 'Task scheduler, must be running for scheduled events',
    'raspotify.service': 'Spotify daemon',
    'shairport-sync.service': "Airplay daemon"
    }


class SystemdService:
    # list(filter(lambda x: 'cloud' in x, subprocess.check_output(['systemctl']).decode().split('\n')))
    # (filter(lambda x: 'cloud' in x, subprocess.check_output(['systemctl', '--user']).decode().split('\n'))))

    def __init__(self, name, user=False):
        self.user = user
        self.name = name
        self.description = SERVICE_DESCRIPTIONS.get(name, 'No description available')

    def _docall(self, arg):
        call = ['systemctl', arg, self.name, '--quiet']
        if self.user:
            call.append('--user')
        return subprocess.call(call, shell=False) == 0

    @property
    def state_string(self):
        return 'running' if self.running else ('failed' if self.failed else 'stopped')

    @property
    def running(self):
        return self._docall('is-active')

    @property
    def failed(self):
        return self._docall('is-failed')

    @property
    def enabled(self):
        return self._docall('is-enabled')

    @property
    def disabled(self):
        return not self.enabled

    def control(self, action):
        """actions as supported by cloud-service-control"""
        getLogger(__name__).info(f'Running cloud-service-control on {self.name}. Command: {action}')
        subprocess.Popen(['/home/pi/.local/bin/cloud-service-control', self.name, action])

    def status_dict(self):
        enabled = self.enabled
        if self.running:
            running = True
            failed = False
        else:
            running = False
            failed = self.failed
        state_string = 'Running' if running else ('Failed' if failed else 'Stopped')

        return {'name': self.name,
                'state': state_string,
                'running': running,
                'failed': failed,
                'enabled': enabled,
                'status': 'enabled' if enabled else 'disabled',
                'toggle_state_command': 'Stop' if running else 'Start',
                'toggle_status_command': 'Disable' if enabled else 'Enable'}

    def systemctl(self, action):
        """Will only work if run from a script with permissions"""
        if self.user:
            print(f'Running systemctl {action} --user {self.name}')
            return subprocess.check_call(['systemctl', action, '--user', self.name])
        else:
            print(f'Running sudo systemctl {action} {self.name}')
            return subprocess.check_call(['sudo', 'systemctl', action, self.name])


def get_services():
    system_services = list(
        map(os.path.basename, glob(pkg_resources.resource_filename('mkidcontrol', '../etc/systemd/system/*'))))
    user_services = list(
        map(os.path.basename, glob(pkg_resources.resource_filename('mkidcontrol', '../systemd-user/*'))))
    l = [SystemdService(s, user=False) for s in system_services] + [SystemdService(s, user=True) for s in user_services]
    return {x.name: x for x in l}


def get_wifi_status(adapter='wlan0'):
    keys = ('SSID', 'Bit Rate', 'Frequency', 'Link Quality', 'Signal level')

    def fetcher(k):
        try:
            s = list(filter(lambda x: k in x, dat))[0]
            return k, (s.split(':')[1] if ':' in s else s.split('=')[1]).strip('"')
        except Exception:
            return k, 'Parse Error'
    try:
        nfo = subprocess.run(['/sbin/iwconfig', adapter], timeout=1, capture_output=True).stdout.decode().split('  ')
        dat = [x for x in map(lambda x: x.strip(), nfo) if x]
        d = dict([fetcher(k) for k in keys])
    except Exception:
        getLogger(__name__).error(f'Failed to poll for {adapter} status', exc_info=True)
        d = {k: 'iwconfig error' for k in keys}

    try:
        ipaddr = subprocess.run(['nmcli', '-f', 'IP4.ADDRESS', 'dev', 'show', adapter], timeout=.2,
                                capture_output=True).stdout.decode().split()[1].strip()
    except:
        ipaddr = 'IP check failed'
    try:
        mac = subprocess.run(['nmcli', '-f', 'GENERAL.HWADDR', 'dev', 'show', adapter], timeout=.2,
                                capture_output=True).stdout.decode().split()[1].strip()
    except:
        ipaddr = 'MAC check failed'

    try:
        ipmode = subprocess.run(['nmcli', '-f', 'IPV4.METHOD', 'con', 'show', d['SSID']], timeout=.2,
                                capture_output=True).stdout.decode().split()[1].strip()
        ipmode = 'dhcp' if ipmode=='auto' else ipmode
    except:
        ipmode= 'mode unknown'

    d['ip'] = f'{ipaddr} ({ipmode}, {mac})'
    return d


def get_system_status(adapter='wlan1'):

    d = get_wifi_status(adapter)

    disks = [(x[0].mountpoint.split('/')[-1], x[1].used / 1024 ** 3, x[1].total / 1024 ** 3) for x in
             [(x, psutil.disk_usage(x.mountpoint)) for x in psutil.disk_partitions()]]
    disks = {x[0]: f'{x[1]:.2f}/{x[2]:.1f} GiB' for x in disks}
    sd = disks.pop('', 'Error')
    for s in ('boot', 'tmp'):
        disks.pop(s, None)
    external = ' '.join(f'{k}: {v}' for k,v in disks.items())
    from datetime import datetime
    up = datetime.now() - datetime.fromtimestamp(psutil.boot_time())

    d.update({'cpu': f'{psutil.getloadavg()[0]/psutil.cpu_count()*100:.0f} % (past minute)',
              'ram': f'{(4.096-psutil.virtual_memory().available/1024**3):.1f}/4.1 GiB',
              'sd': sd, 'ext_disks': external,
              'uptime': f'{up.days} day(s) {(up.total_seconds()/3600-up.days*24):.2f} hours'})
    return d


def get_service(name):
    system_services = list(
        map(os.path.basename, glob(pkg_resources.resource_filename('mkidcontrol', '../etc/systemd/*'))))
    user_services = list(
        map(os.path.basename, glob(pkg_resources.resource_filename('mkidcontrol', '../systemd-user/*'))))
    if name not in system_services + user_services:
        raise ValueError('Unknown service')
    return SystemdService(name, user=name in user_services)
