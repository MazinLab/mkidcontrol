from mkidcontrol.packetmaster3.packetmaster import Packetmaster

roachNums = [115,....]
captureport = 50000
offline = False
imgcfg = {'dashboard': dict(use_wave=False, wave_start=700, wave_stop=1500, n_wave_bins=1)}
beammap = mkidcore.beammap.Beammap()  #beammap object
wavecal_file = '/home/baileyji/mec/2019-01-13 10529f0c026d91f8361e8d1bbe93699fcccb.npz'
bindir = '/some/path'

packetmaster = Packetmaster(len(roachNums), captureport, useWriter=not offline, sharedImageCfg=imgcfg,
                                 beammap=beammap, forwarding=None, recreate_images=True)

wvl_coeffs = np.load(wavecal_file)
packetmaster.applyWvlSol(wvlCoeffs, beammap)

liveimage = packetmaster.sharedImages['dashboard']

liveimage.startIntegration(startTime=time.time() - SHAREDIMAGE_LATENCY, integrationTime=1)
data = liveimage.receiveImage()

liveimage.set_useWvl(True)


packetmaster.startWriting(binDir=bindir)
packetmaster.stopWriting()
