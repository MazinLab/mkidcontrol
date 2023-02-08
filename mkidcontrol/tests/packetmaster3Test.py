import os.path

from mkidcontrol.packetmaster3.packetmaster import Packetmaster
import time
from mkidcore.objects import Beammap
import numpy as np
import logging
import datetime
from astropy.io import fits

logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger(__name__)

SHAREDIMAGE_LATENCY = 0.55 #0.53 #latency fudge factor for sharedmem, taken from dashboard.py

roachNums = [115, 116, 117, 118, 119, 120, 121, 122]
captureport = 50000
offline = False
imgcfg = {'dashboard': dict(use_wave=False, wave_start=700, wave_stop=1500, n_wave_bins=1)}
beammap = Beammap(specifier="XKID")  #beammap object
wavecal_file = '/home/kids/mkidcontrol/mkidcontrol/tests/wavecals/wavecal0_4b22ee4c_f4d1f81f.wavecal.npz'
bindir = '/some/path'

packetmaster = Packetmaster(len(roachNums), captureport, useWriter=not offline, sharedImageCfg=imgcfg,
                                 beammap=beammap, forwarding=None, recreate_images=True)

# TODO: These lines break because you need an older-style wavecal. Need to look into this
# wvl_coeffs = np.load(wavecal_file)
# packetmaster.applyWvlSol(wvl_coeffs, beammap)

liveimage = packetmaster.sharedImages['dashboard']

liveimage.startIntegration(startTime=time.time() - SHAREDIMAGE_LATENCY, integrationTime=1)
data = liveimage.receiveImage()

liveimage.set_useWvl(True)

packetmaster.startWriting(binDir=bindir)
packetmaster.stopWriting()