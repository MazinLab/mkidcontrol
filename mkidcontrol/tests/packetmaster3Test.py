import os.path

from mkidcontrol.packetmaster3.packetmaster import Packetmaster
import time
from mkidcore.objects import Beammap
import numpy as np
import logging
import datetime
from astropy.io import fits
import threading

logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger(__name__)

SHAREDIMAGE_LATENCY = 0.55 #0.53 #latency fudge factor for sharedmem, taken from dashboard.py

roachNums = [115, 116, 117, 118, 119, 120, 121, 122]
captureport = 50000
offline = False
imgcfg = {'dashboard': dict(use_wave=False, wave_start=700, wave_stop=1500, n_wave_bins=1)}
beammap = Beammap(specifier="XKID")  #beammap object
wavecal_file = '/home/kids/mkidcontrol/mkidcontrol/tests/wavecals/wavecal0_4b22ee4c_f4d1f81f.wavecal.npz'
bindir = '/data/XKID/testdata'

if not os.path.exists(bindir):
    os.mkdir(bindir)

class LiveImageFetcher:
    def __init__(self, sharedim, inttime=1, offline=False):
        self.imagebuffer = sharedim
        self.inttime = inttime
        self.search = True
        self.offline_mode = offline  #zeros ok

    def update_inttime(self, it):
        self.inttime = float(it)

    def run(self):
        self.search = True
        while self.search:
            try:
                utc = datetime.datetime.utcfromtimestamp(time.time()).strftime('%Y-%m-%d %H:%M:%S')
                self.imagebuffer.startIntegration(integrationTime=self.inttime)
                data = self.imagebuffer.receiveImage()
                if not data.sum() and not self.offline_mode:
                    log.warning('Received a frame of zeros from packetmaster!')
                ret = fits.ImageHDU(data=data)
                ret.header['utcstart'] = utc
                ret.header['exptime'] = self.inttime
                try:
                    ret.header['wavecal'] = self.imagebuffer.wavecalID.decode('UTF-8', "backslashreplace")
                except AttributeError:
                    ret.header['wavecal'] = self.imagebuffer.wavecalID

                if ret.header['wavecal']:
                    ret.header['wmin'] = self.imagebuffer.wvlStart
                    ret.header['wmax'] = self.imagebuffer.wvlStop
                else:
                    ret.header['wmin'] = 'NaN'
                    ret.header['wmax'] = 'NaN'
                yield ret
            except RuntimeError as e:
                log.debug('Image stream unavailable: {}'.format(e))
            except Exception:
                log.error('Problem', exc_info=True)


if __name__ == "__main__":
    packetmaster = Packetmaster(len(roachNums), captureport, useWriter=not offline, sharedImageCfg=imgcfg,
                                beammap=beammap, forwarding=None, recreate_images=True)

    # TODO: These lines break because you need an older-style wavecal. Need to look into this
    # wvl_coeffs = np.load(wavecal_file)
    # packetmaster.applyWvlSol(wvl_coeffs, beammap)

    packetmaster.startWriting(binDir=bindir)

    li = packetmaster.sharedImages['dashboard']

    li.startIntegration(startTime=time.time() - SHAREDIMAGE_LATENCY, integrationTime=1)
    d = li.receiveImage()

    # TODO: Seeing long latency in the receiveImage() function. Unsure why and don't have time to troubleshoot
    while True:
        try:
            s = time.time()
            li.startIntegration(integrationTime=1)
            m = time.time()
            d = li.receiveImage()
            e = time.time()
            print(m - s)
            print(e - m)
        except Exception as e:
            break

    print('here')

    packetmaster.stopWriting()
