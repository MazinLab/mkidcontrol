from mkidcontrol.packetmaster3.sharedmem import ImageCube
import numpy as np
import time
from logging import getLogger
from astropy.io import fits
CURRENT_DARK_FILE_KEY = "datasaver:dark"
CURRENT_FLAT_FILE_KEY = "datasaver:flat"
IMAGE_BUFFER_NAME = 'live'


def live_image_fetcher(app, redis, dashcfg):
    d = {CURRENT_DARK_FILE_KEY: '', CURRENT_FLAT_FILE_KEY: ''}
    mask = dashcfg.beammap.failmask
    dark_cps = np.zeros_like(mask, dtype=float)
    flat_cps = np.ones_like(mask, dtype=float)
    log = getLogger(__name__)
    live = ImageCube(name=IMAGE_BUFFER_NAME, nRows=dashcfg.beammap.nrows, nCols=dashcfg.beammap.ncols,
                     useWvl=dashcfg.dashboard.use_wave, nWvlBins=1, wvlStart=dashcfg.dashboard.wave_start,
                     wvlStop=dashcfg.dashboard.wave_stop)
    while True:
        events = app.image_events
        if not events:
            time.sleep(.3)
            continue
        d_new = redis.read((CURRENT_DARK_FILE_KEY, CURRENT_FLAT_FILE_KEY))
        int_time = app.array_view_params['int_time']
        image_watcher_events = app.image_events

        if d_new[CURRENT_DARK_FILE_KEY] != d[CURRENT_DARK_FILE_KEY]:
            d[CURRENT_DARK_FILE_KEY] = d_new[CURRENT_DARK_FILE_KEY]
            if not d[CURRENT_DARK_FILE_KEY]:
                dark_cps[:] = 0
            else:
                try:
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
                    flat = fits.open(d[CURRENT_FLAT_FILE_KEY])[0]
                    flat_cps[:] = flat.data / flat.header['EXPTIME']
                    del flat
                except IOError:
                    log.warning(f'Unable to read {d[CURRENT_FLAT_FILE_KEY]}, using 1s for flat. '
                                f'Change flat to try again')
                    flat_cps[:] = 1

        live.startIntegration(startTime=0, integrationTime=int_time)
        im = live.receiveImage(block=True)

        data = (im / int_time - dark_cps) / flat_cps
        data[mask] = np.nan

        app.latest_image[:] = data
        for e in image_watcher_events:
            e.set()
