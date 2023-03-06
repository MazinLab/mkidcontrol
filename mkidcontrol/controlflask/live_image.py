from mkidcontrol.packetmaster3.sharedmem import ImageCube
import numpy as np
import time
from logging import getLogger
from astropy.io import fits
import warnings
CURRENT_DARK_FILE_KEY = "datasaver:dark"
CURRENT_FLAT_FILE_KEY = "datasaver:flat"
IMAGE_BUFFER_NAME = 'live'


def live_image_fetcher(app, redis, dashcfg):
    d = {CURRENT_DARK_FILE_KEY: '', CURRENT_FLAT_FILE_KEY: ''}
    mask = dashcfg.beammap.failmask
    dark_cps = np.zeros_like(mask, dtype=float)
    flat_cps = np.ones_like(mask, dtype=float)
    log = getLogger(__name__)
    log.propagate = True
    log.setLevel('DEBUG')
    live = ImageCube(name=IMAGE_BUFFER_NAME, nRows=dashcfg.beammap.nrows, nCols=dashcfg.beammap.ncols,
                     useWvl=dashcfg.dashboard.use_wave, nWvlBins=1, wvlStart=dashcfg.dashboard.wave_start,
                     wvlStop=dashcfg.dashboard.wave_stop)
    dur=count=dur1=dur2=0
    while True:
        events = app.image_events
        if not events:
            time.sleep(.3)
            continue
        tic = time.time()
        d_new = redis.read((CURRENT_DARK_FILE_KEY, CURRENT_FLAT_FILE_KEY))
        int_time = app.array_view_params['int_time']
        image_watcher_events = app.image_events

        if d_new[CURRENT_DARK_FILE_KEY] != d[CURRENT_DARK_FILE_KEY]:
            d[CURRENT_DARK_FILE_KEY] = d_new[CURRENT_DARK_FILE_KEY]
            if not d[CURRENT_DARK_FILE_KEY]:
                dark_cps[:] = 0
            else:
                try:
                    log.info(f'Loading flat {d[CURRENT_DARK_FILE_KEY]}')
                    dark = fits.open(d[CURRENT_DARK_FILE_KEY])[0]
                    dark_cps[:] = dark.data / dark.header['EXPTIME']
                    del dark
                except IOError:
                    log.warning(f'Unable to read {d[CURRENT_DARK_FILE_KEY]}, using 0s for dark. '
                                f'Change dark to try again')
                    dark_cps[:] = 0

        if d_new[CURRENT_FLAT_FILE_KEY] != d[CURRENT_FLAT_FILE_KEY]:
            d[CURRENT_FLAT_FILE_KEY] = d_new[CURRENT_FLAT_FILE_KEY]
            if not d[CURRENT_FLAT_FILE_KEY]:
                flat_cps[:] = 1
            else:
                try:
                    log.info(f'Loading flat {d[CURRENT_FLAT_FILE_KEY]}')
                    flat = fits.open(d[CURRENT_FLAT_FILE_KEY])[0]
                    flat_cps[:] = flat.data / flat.header['EXPTIME']
                    flat_cps[flat_cps==0]=1
                    del flat
                except IOError:
                    log.warning(f'Unable to read {d[CURRENT_FLAT_FILE_KEY]}, using 1s for flat. '
                                f'Change flat to try again')
                    flat_cps[:] = 1

        itime=max(int_time, 1/30)
        tic2 = time.time()
        live.startIntegration(startTime=0, integrationTime=itime)
        im = live.receiveImage(timeout=False)
        toc2 = time.time()

        tic1 = time.time()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            data = (im / itime - dark_cps) / flat_cps
            data[mask] = 0
        app.latest_image[:] = data
        toc1=time.time()

        toc=time.time()
        dur += toc-tic
        dur1+=toc1-tic1
        dur2+=toc2-tic2
        count+=1
        if count>=30:
            log.info(f'Live image using dark ({dark_cps.min():.2f}-{dark_cps.max():.2f}) '
                     f'and flat ({flat_cps.min():.2f}-{flat_cps.max():.2f}) resulting in an image '
                     f'with {data.min():.2f}-{data.max():.2f} photons/s')
            log.info(f'FPS attained {count/dur:.2f}')
            log.info(f'Processing Time: {dur1/count*1000:.3f} ms')
            log.info(f'Acq Time: {dur2 / count:.3f} s')
            dur=count=dur1=dur2=0
        for e in image_watcher_events:
            e.set()
