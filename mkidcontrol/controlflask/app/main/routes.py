import sys
import os
import shutil
import subprocess
from datetime import datetime
import plotly.graph_objects as go
from astropy.io import fits

from flask import render_template, flash, redirect, url_for, g, request, \
    jsonify, current_app, Response, stream_with_context
from flask_login import current_user, login_required
from flask_babel import _, get_locale

from .. import db
# from .forms import *
from ..models import Notification
from . import bp
from .helpers import *
from ..api.errors import bad_request
import time
import plotly
from datetime import timedelta
import datetime
import json
from rq.job import Job, NoSuchJobError
import select

from mkidcore.fits import CalFactory

from mkidcontrol.mkidredis import RedisError
from mkidcontrol.util import setup_logging
from mkidcontrol.util import get_service as mkidcontrol_service
from mkidcontrol.util import get_services as mkidcontrol_services

import mkidcontrol.mkidredis as redis
from mkidcontrol.commands import COMMAND_DICT, LakeShoreCommand, FILTERS
from mkidcontrol.config import FLASK_KEYS, REDIS_TS_KEYS, FLASK_CHART_KEYS

from mkidcontrol.controlflask.app.main.forms import *

from mkidcontrol.agents.xkid.observingAgent import OBSERVING_EVENT_KEY

# TODO: ObsLog, ditherlog, dashboardlog

# TODO: Add redis key capturing whether we are observing or not!

# TODO: With the GUI it needs to pass the 'at a glance test' -> the user should be able to tell whats going on from a simple look
#  Think "green for good, red for error", good compartmentalization (spacing on page and similar things go together), less clutter

# TODO: Command handling

# TODO: Form submission only changes changed values (e.g. don't change Curve No. = 8 -> Curve No. = 8)

# TODO: MUST TEST FOR CONCURRENCY ISSUES (Controlling the instrument from multiple tabs, does it work, does it stay in
#  sync?), do pages need to reload to show status updates or do they just update?

# TODO: Work with auto-discovery where possible (for keys/programs/etc)


CURRENT_DARK_FILE_KEY = "datasaver:dark"
CURRENT_FLAT_FILE_KEY = "datasaver:flat"

redis.setup_redis(ts_keys=REDIS_TS_KEYS)

log = setup_logging('controlDirector')


def guess_language(x):
    return 'en'


@bp.before_app_request
def before_request():
    if current_user.is_authenticated:
        current_user.last_seen = datetime.datetime.utcnow()
        db.session.commit()
    g.locale = str(get_locale())
    g.redis = current_app.redis
    # if current_app.redis:
    #     g.redis = current_app.redis
    # else:
    #     g.redis = mkidcontrol.mkidredis.setup_redis(ts_keys=TS_KEYS)


@bp.after_request
def add_header(response):
    response.headers['Cache-Control'] = 'no-cache, no-store'
    return response


@bp.route('/', methods=['GET', 'POST'])
@bp.route('/main', methods=['GET', 'POST'])
def index():
    """
    Flask endpoint for the main app page.
    Processes requests from the magnet cycle form (start/abort/cancel/schedule cooldown) and magnet form (ramp rates/
    soak settings) and publishes them to be interpreted by the necessary agents.
    Initializes sensor plot data to send for plotting.
    TODO: Handle 'post' requests - via AJAX requests rather than submitting
    TODO: Support message flashing
    """
    try:
        current_app.redis.read(FLASK_KEYS)
    except RedisError:
        return redirect(url_for('main.redis_error_page'))
    except KeyError:
        flash(f"Redis keys are missing!")

    # TODO: Parse in current values at startup when endpoint gets hit
    from mkidcontrol.commands import Laserbox, Filterwheel, Focus

    magnetform = MagnetCycleForm()  # TODO: Should start ramp pull up a modal with settings?
    hsform = HeatSwitchForm()
    laserbox = LaserBoxForm(**vars(Laserbox(redis)))
    fw = FilterWheelForm(**vars(Filterwheel(redis)))
    focus = FocusForm(**vars(Focus(redis)))
    obs = ObsControlForm()
    conex = ConexForm()

    sending_photons = True if (current_app.redis.read(OBSERVING_EVENT_KEY, decode_json=True)['state'].lower() == "started") else False
    cooldown_scheduled = True if (current_app.redis.read('device-settings:magnet:cooldown-scheduled').lower() == "yes") else False
    if cooldown_scheduled:
        cooldown_time = float(current_app.redis.read('device-settings:magnet:cooldown-scheduled:timestamp'))
        cooldown_time = datetime.datetime.fromtimestamp(cooldown_time).strptime('%Y-%m-%dT%H:%M')
    else:
        cooldown_time = (datetime.date.today()+timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')

    form = FlaskForm()

    sensor_fig = multi_sensor_fig(FLASK_CHART_KEYS.keys())
    array_fig = initialize_array_figure(current_app.array_view_params)
    pix_lightcurve = pixel_lightcurve()

    return render_template('index.html', sending_photons=sending_photons, magnetform=magnetform, hsform=hsform, fw=fw,
                           focus=focus, form=form, laserbox=laserbox, obs=obs, conex=conex, sensor_fig=sensor_fig,
                           array_fig=array_fig, cooldown_scheduled=cooldown_scheduled, cooldown_time=cooldown_time,
                           pix_lightcurve=pix_lightcurve, sensorkeys=list(FLASK_CHART_KEYS.values()))


@bp.route('/other_plots', methods=['GET'])
def other_plots():
    """
    Flask endpoint for 'other plots'. This page has ALL sensor plots in one place for convenience (in contrast to index,
    which only has one at a time).
    """

    form = FlaskForm()

    plots = [create_fig(title) for title in FLASK_CHART_KEYS.keys()]

    ids = ['device_t', 'device_r',
           'onek_t', 'onek_r',
           'threek_t', 'threek_v',
           'fiftyk_t', 'fiftyk_v',
           'magnet_i', 'magnet_f',
           'ls625_ov']

    return render_template('other_plots.html', title=_('Other Plots'), form=form,
                           plots=plots, ids=ids, sensorkeys=list(FLASK_CHART_KEYS.values()))


@bp.route('/log_viewer', methods=['GET', 'POST'])
def log_viewer():
    """
    Flask endpoint for log viewer. This page is solely for observing the journalctl output from each agent.
    # TODO: Update for xkid logs
    """
    form = FlaskForm()
    return render_template('log_viewer.html', title=_('Log Viewer'), form=form)


@bp.route('/heater/<device>/<channel>', methods=['GET', 'POST'])
def heater(device, channel):

    if request.method == 'POST':
        for key in request.form.keys():
            try:
                x = LakeShoreCommand(
                    f"device-settings:{device}:heater-channel-{request.form.get('channel').lower()}:{key.replace('_', '-')}",
                    request.form.get(key))
                log.info(f"Sending command:{x.setting}' -> {x.value} ")
                msg_listeners = current_app.redis.publish(f"command:{x.setting}", x.value, store=False)
                log.info(f"Command sent successfully, heard by {msg_listeners} listeners")
            except ValueError as e:
                log.warning(f"Value error: {e} in parsing commands")
                log.debug(f"Unrecognized field to send as command: {key}")
            time.sleep(0.15)

    if device == "ls372":
        from mkidcontrol.controlflask.app.main.forms import OutputHeaterForm
        from ....commands import LS372HeaterOutput
        heater = LS372HeaterOutput(channel, redis)

        if channel == "0":
            title = "Sample Heater"
            form = OutputHeaterForm(**vars(heater))
        elif channel == "1":
            title = "Warm-Up Heater"
            return redirect(url_for('main.page_not_found'))
        elif channel == "2":
            title = "Analog/Still"
            return redirect(url_for('main.page_not_found'))
    elif device == "ls336":
        return redirect(url_for('main.page_not_found'))
    else:
        return redirect(url_for('main.page_not_found'))
    return render_template('heater.html', title=_(f"{title} Control"), form=form)


@bp.route('/thermometry/<device>/<channel>/<filter>', methods=['GET', 'POST'])
def thermometry(device, channel, filter):
    try:
        title = current_app.redis.read(f'device-settings:{device}:input-channel-{channel.lower()}:name')
    except:
        return redirect(url_for('main.page_not_found'))

    if request.method == 'POST':
        for key in request.form.keys():
            try:
                x = LakeShoreCommand(
                    f"device-settings:{device}:input-channel-{request.form.get('channel').lower()}:{key.replace('_', '-')}",
                    request.form.get(key))
                log.info(f"Sending command:{x.setting}' -> {x.value} ")
                msg_listeners = current_app.redis.publish(f"command:{x.setting}", x.value, store=False)
                log.info(f"Command sent successfully, heard by {msg_listeners} listeners")
            except ValueError as e:
                log.warning(f"Value error: {e} in parsing commands")
                log.debug(f"Unrecognized field to send as command: {key}")
            time.sleep(.15)

    # TODO: Turn all of this if/else into a single 'thermometry' form
    if device == 'ls336':
        from mkidcontrol.commands import LS336InputSensor

        sensor = LS336InputSensor(channel=channel, redis=current_app.redis)
        if sensor.sensor_type == "NTC RTD":
            form = RTDForm(**vars(sensor))
        elif sensor.sensor_type == "Diode":
            form = DiodeForm(**vars(sensor))
        elif sensor.sensor_type == "Disabled":
            form = DisabledInput336Form(**vars(sensor))
    elif device == 'ls372':
        from mkidcontrol.commands import LS372InputSensor, ALLOWED_372_INPUT_CHANNELS
        sensor = LS372InputSensor(channel=channel, redis=current_app.redis)
        if sensor.enable == "True":
            if channel == "A":
                if filter == "filter":
                    form = Input372FilterForm(**vars(sensor))
                else:
                    form = ControlSensorForm(**vars(sensor))
            elif channel in ALLOWED_372_INPUT_CHANNELS[1:]:
                if filter == "filter":
                    form = Input372FilterForm(**vars(sensor))
                else:
                    form = Input372SensorForm(**vars(sensor))
        else:
            if channel == "A":
                form = DiasbledControlSensorForm(**vars(sensor))
            elif channel in ALLOWED_372_INPUT_CHANNELS[1:]:
                form = DisabledInput372SensorForm(**vars(sensor))
    else:
        return redirect(url_for('main.page_not_found'))

    return render_template('thermometry.html', title=_(f"{title} Thermometer"), form=form)


@bp.route('/cycle_settings', methods=['POST', 'GET'])
def cycle_settings():
    from mkidcontrol.commands import MagnetCycleSettings

    cyclesettings = MagnetCycleSettings(current_app.redis)

    if request.method == 'POST':
        for key in request.form.keys():
            try:
                x = LakeShoreCommand(f"device-settings:magnet:{key.replace('_', '-')}", request.form.get(key))
                log.info(f"Sending command:{x.setting}' -> {x.value} ")
                msg_listeners = current_app.redis.publish(f"command:{x.setting}", x.value, store=False)
                log.info(f"Command sent successfully, heard by {msg_listeners} listeners")
            except ValueError as e:
                log.warning(f"Value error: {e} in parsing commands")
                log.debug(f"Unrecognized field to send as command: {key}")
            time.sleep(0.15)

    form = MagnetCycleSettingsForm(**(vars(cyclesettings)))

    return render_template('cycle_settings.html', title=_("Cooldown Cycle Settings"), form=form)


@bp.route('/ls625', methods=['POST', 'GET'])
def ls625():
    from mkidcontrol.commands import LS625MagnetSettings

    ls625settings = LS625MagnetSettings(current_app.redis)

    if request.method == 'POST':
        for key in request.form.keys():
            try:
                x = LakeShoreCommand(f"device-settings:ls625:{key.replace('_', '-')}", request.form.get(key),
                                     limit_vals=ls625settings.limits)
                log.info(f"Sending command:{x.setting}' -> {x.value} ")
                msg_listeners = current_app.redis.publish(f"command:{x.setting}", x.value, store=False)
                log.info(f"Command sent successfully, heard by {msg_listeners} listeners")
            except ValueError as e:
                log.warning(f"Value error: {e} in parsing commands")
                log.debug(f"Unrecognized field to send as command: {key}")
            time.sleep(0.15)

    form = Lakeshore625ControlForm(**vars(ls625settings))

    return render_template('ls625.html', title=_("Magnet Power Supply Control"), form=form)


@bp.route('/heatswitch/', methods=['POST', 'GET'])
def heatswitch():
    from mkidcontrol.commands import Heatswitch

    hs = Heatswitch(current_app.redis)

    if request.method == "POST":
        for key in request.form.keys():
            try:
                x = LakeShoreCommand(f"device-settings:heatswitch:{key.replace('_', '-')}", request.form.get(key))
                log.info(f"Sending command:{x.setting}' -> {x.value} ")
                msg_listeners = current_app.redis.publish(f"command:{x.setting}", x.value, store=False)
                log.info(f"Command sent successfully, heard by {msg_listeners} listeners")
            except ValueError as e:
                log.warning(f"Value error: {e} in parsing commands")
                log.debug(f"Unrecognized field to send as command: {key}")
            time.sleep(0.15)

    form = HeatSwitchForm(**vars(hs))

    return render_template('heatswitch.html', title=_('Heat Switch'), form=form)


@bp.route('/services')
def services():
    #TODO: Fix Job.fetch(id, ...), id='email-logs' does not work
    services = mkidcontrol_services()
    try:
        job = Job.fetch('email-logs', connection=current_app.redis.redis)
        exporting = job.get_status() in ('queued', 'started', 'deferred', 'scheduled')
    except NoSuchJobError:
        exporting = False
    return render_template('services.html', title=_('Services'), services=services.values(), exporting=exporting)


@bp.route('/service', methods=['POST', 'GET'])
def service():
    """start, stop, enable, disable, restart"""
    name = request.args.get('name', '')
    try:
        service = mkidcontrol_service(name)
    except ValueError:
        return bad_request(f'Service "{name}" does not exist.')
    if request.method == 'POST':
        service.control(request.form['data'])
        # flash('Executing... updating in 5')
        return jsonify({'success': True})
    else:
        return jsonify(service.status_dict())


@bp.route('/task', methods=['GET', 'POST'])
def task():
    if request.method == 'POST':
        id = request.form.get('id')
        if id != 'email-logs':
            return bad_request('Unknown task')
        try:
            job = Job.fetch(id, connection=g.redis.redis)
            if job.is_failed or job.is_finished:
                job.delete()
                job = None
        except NoSuchJobError:
            job = None
        if job:
            flash(_(f'Task "{id} is currently pending'))
            return bad_request(f'Task "{id}" in progress')
        else:
            current_app.task_queue.enqueue(f"mkidcontrol.controlflask.app.tasks.{id.replace('-', '_')}", job_id=id)
            return jsonify({'success': True})

    else:
        id = request.args.get('id', '')
        if not id:
            return bad_request('Task id required')
        try:
            job = Job.fetch(id, connection=g.redis.redis)
        except NoSuchJobError:
            return bad_request('Unknown task')
        status = job.get_status()
        return jsonify({'done': status == 'finished', 'error': status != 'finished',
                        'progress': job.meta.get('progress', 0)})


# NOTE (N.S.) 19 July 2022: I'm disinclined to include this as a route and leave it to only be allowable by a
# responsible superuser
@bp.route('/system', methods=['POST'])
def system():
    """data: shutdown|reboot|reinit """
    cmd = request.form.get('data', '')
    if cmd in ('shutdown', 'reboot'):
        # TODO: Shutdown/reboot command
        return bad_request('Invalid shutdown command')
        # subprocess.Popen(['/home/kids/.local/bin/mkid-service-control', cmd])
        # flash(f'System going offline for {cmd}')
        # return jsonify({'success': True})
    elif cmd == 'reinit':
        import mkidcontrol.redis as redis
        redis.setup_redis(ts_keys=REDIS_TS_KEYS)
        return jsonify({'success': True})
    else:
        return bad_request('Invalid shutdown command')


@bp.route('/new_night', methods=['GET', 'POST'])
def new_night():
    # TODO: Turn new night & configuration into one page once further stabilized & tested
    newnight = NewNightForm()
    ymls_to_copy = ['dashboard.yml', 'hightemplar.yml', 'roaches.yml']

    if request.method == "POST":
        current_dir = redis.read('paths:data-dir')
        base_dir = current_app.base_dir

        newdate = datetime.datetime.utcnow().strftime("%Y%m%d")
        new_night_dir = os.path.join(base_dir, f"ut{newdate}")
        try:
            os.mkdir(new_night_dir)
        except FileExistsError:
            log.info(f"The directory for the new night ({new_night_dir} already exists!")

        for yml in ymls_to_copy:
            shutil.copy(os.path.join(current_dir, yml), new_night_dir)

        try:
            redis.store({'paths:data-dir': new_night_dir})
            redis.store({'gen2-dashboard-yaml': os.path.join(new_night_dir, 'dashboard.yml')})
        except RedisError as e:
            log.warning(f"Error communicating with redis! Error: {e}")


        print(datetime.datetime.now().strftime("%Y%m%d"))
    return render_template('new_night.html', title=_('Configuration Paths'), newnight=newnight)


@bp.route('/configuration_paths', methods=['GET', 'POST'])
def configuration_paths():
    cfgform = ConfigPathForm()
    return render_template('configuration_paths.html', title=_('Configuration Paths'), cfgForm=cfgform)


@bp.route('/test_page', methods=['GET', 'POST'])
def test_page():
    from mkidcontrol.controlflask.app.main.forms import MagnetCycleSettingsForm
    if request.method == "POST":
        print(request.form)
    from mkidcontrol.commands import Laserbox
    l = Laserbox(redis)
    form = LaserBoxForm(**vars(l))

    return render_template('test_page.html', title=_('Test Page'), form=form)


@bp.route('/404', methods=['GET', 'POST'])
def page_not_found():
    return render_template('/errors/404.html'), 404


@bp.route('/6379', methods=['GET', 'POST'])
def redis_error_page():
    return render_template('/errors/6379.html'), 412


# ----------------------------------- Helper Functions Below -----------------------------------
@bp.route('/listener', methods=["GET"])
def listener():
    """
    listener is a function that implements the python (server) side of a server sent event (SSE) communication protocol
    where data can be streamed directly to the flask app.
    """

    @stream_with_context
    def _stream():
        while True:
            time.sleep(.5)
            x = current_app.redis.read(FLASK_KEYS)
            y = mkidcontrol_services().items()
            s = {}
            for k, v in y:
                sd = v.status_dict()
                if sd['enabled']:
                    if sd['running']:
                        s.update({k: 'Running'})
                    elif sd['failed']:
                        s.update({k: 'Failed'})
                else:
                    s.update({k: 'Disabled'})

            x.update(s)
            x = json.dumps(x)
            msg = f"retry:5\ndata: {x}\n\n"
            yield msg

    return current_app.response_class(_stream(), mimetype='text/event-stream', content_type='text/event-stream')


@bp.route('/journalctl_streamer/<service>')
def journalctl_streamer(service):
    """
    journalctl streamer is another SSE server-side function. The name of an agent (or systemd service, they are the
    same) is passed as an argument and the log messages from that service will then be streamed to wherever this
    endpoint is called.
    """
    args = ['journalctl', '--lines', '0', '--follow', f'_SYSTEMD_UNIT={service}.service']
    print(service)
    def st(arg):
        f = subprocess.Popen(arg, stdout=subprocess.PIPE)
        p = select.poll()
        p.register(f.stdout)
        while True:
            if p.poll(100):
                line = f.stdout.readline()
                yield f"retry:5\ndata: {line.strip().decode('utf-8')}\n\n"

    return Response(st(args), mimetype='text/event-stream', content_type='text/event-stream')


@bp.route('/pixel_lightcurve', methods=["POST"])
def pixel_lightcurve(init=True, time=None, cts=None, pix_x=-1, pix_y=-1):
    if request.method == "POST":
        init = bool(int(request.form.get("init")))
        time = request.form.get("time")
        cts = float(request.form.get("cts"))
        pix_x = int(request.form.get("pix_x"))
        pix_y = int(request.form.get("pix_y"))

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=[time], y=[cts], mode='lines'))
    if init:
        fig.update_layout(title=f"Pixel Not Selected")
    else:
        fig.update_layout(title=f"Pixel ({pix_x}, {pix_y})")  # , xaxis=dict(tickangle=0, nticks=3))
    fig = json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)
    return fig


def create_fig(name):
    since = None
    first_tval = int((datetime.datetime.now() - timedelta(hours=5)).timestamp() * 1000) if not since else since
    timestream = np.array(current_app.redis.mkr_range(FLASK_CHART_KEYS[name], f"{first_tval}"))
    if timestream[0][0] is not None:
        times = [datetime.datetime.fromtimestamp(t / 1000).strftime("%m/%d/%Y %H:%M:%S") for t in timestream[:, 0]]
        vals = list(timestream[:, 1])
    else:
        times = [None]
        vals = [None]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=times, y=vals, mode='lines', name=f"{name}"))
    fig.update_layout(dict(title=f"{name}", xaxis=dict(tickangle=0, nticks=2)))
    fig = json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)
    return fig


def multi_sensor_fig(titles):
    since = None
    first_tval = int((datetime.datetime.now() - timedelta(hours=0.5)).timestamp() * 1000) if not since else since
    keys = [FLASK_CHART_KEYS[title] for title in titles]

    timestreams = [np.array(current_app.redis.mkr_range(key, f"{first_tval}")) for key in keys]
    times = []
    for ts in timestreams:
        if ts[0][0] is not None:
            times.append([datetime.datetime.fromtimestamp(t / 1000).strftime("%m/%d/%Y %H:%M:%S") for t in ts[:, 0]])
        else:
            times.append([None])
    vals = [list(ts[:, 1]) for ts in timestreams]

    update_menus = []
    for n, t in enumerate(titles):
        visible = [False] * len(titles)
        visible[n] = True
        t_dict = dict(label=str(t),
                      method='update',
                      args=[{'visible': visible}])
        update_menus.append(t_dict)

    fig = go.Figure()
    for count, data in enumerate(zip(times, vals, titles)):
        if count == 0:
            fig.add_trace(go.Scatter(x=data[0], y=data[1], mode='lines', name=data[2], visible=True))
        else:
            fig.add_trace(go.Scatter(x=data[0], y=data[1], mode='lines', name=data[2], visible=False))
    fig.update_layout(
        dict(updatemenus=list([dict(buttons=update_menus, x=0.01, xanchor='left', y=1.1, yanchor='top')])))
    fig = json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)
    return fig


def initialize_array_figure(view_params):
    """
    Creates the graph object for the first frame of array data shown.
    This will always be an array of zeros.
    """
    y = np.zeros((125, 80))
    fig = go.Figure()
    fig.add_heatmap(z=y.tolist(), showscale=False, colorscale=[[0, "black"], [0.5, "white"], [0.5, "red"], [1, "red"]],
                    zmin=view_params['min_cts'], zmax=view_params['max_cts'] * 2)
    fig.update_layout(dict(height=550, autosize=True, xaxis=dict(visible=False, ticks='', scaleanchor='y'),
                           yaxis=dict(visible=False, ticks='')))
    fig.update_layout(margin=dict(l=0, r=0, b=0, t=0, pad=3))
    fig = json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)
    return fig


@bp.route('/dashplot', methods=["GET"])
def dashplot():
    """
    TODO: Ingest dark/flats and apply
        #From darshboard
        # # Set up worker object and thread for the display.
        # #  All of this code could be axed if the live image was broken out into a separate program
        # cf = CalFactory('avg', images=self.imageList[-1:],
        #                 dark=self.darkField if self.checkbox_darkImage.isChecked() else None,
        #                 flat=self.flatField if self.checkbox_flatImage.isChecked() else None,
        #                 mask=self.beammapFailed)
        vals = cf.generate(name='LiveImage', bias=0, maskvalue=np.nan).data
        also
        # cf = CalFactory('sum', images=self.imageList[-numImages2Sum:], dark=self.darkField if applyDark else None)
        # im = cf.generate(name='pixelcount')
        # pixelList = np.asarray(pixelList)
        # im.data[(pixelList[:, 1], pixelList[:, 0])].sum()

    TODO: Why is restyle still so slow on the GUI side even when doing a partial remake?
    """

    @stream_with_context
    def _stream():
        active_dark_file = current_app.redis.read(CURRENT_DARK_FILE_KEY)
        active_flat_file = current_app.redis.read(CURRENT_FLAT_FILE_KEY)
        sciFac = CalFactory('avg',
                            dark=fits.open(active_dark_file) if os.path.exists(active_dark_file) else None,
                            flat=fits.open(active_flat_file) if os.path.exists(active_flat_file) else None)
        while True:
            int_time = current_app.array_view_params['int_time']
            current_dark_file = current_app.redis.read(CURRENT_DARK_FILE_KEY)
            current_flat_file = current_app.redis.read(CURRENT_FLAT_FILE_KEY)

            # TODO: When live (toggle online/offline here? So we can pull up the gui without roaches and annoyingly long image load lags?)
            current_app.liveimage.startIntegration(start=0, integrationTime=int_time)
            # t = time.time()
            im = current_app.liveimage.receiveImage()

            # im = np.random.uniform(5000, size=(125 * 80)).reshape((125, 80)).astype(int)
            # im = im.tolist()
            # time.sleep(int_time)

            tic = time.time()
            # if active_dark_file != current_dark_file:
            #     active_dark_file = current_dark_file
            #     del sciFac.dark
            #     sciFac.dark = fits.open(active_dark_file) if os.path.exists(active_dark_file) else None
            #
            # if active_flat_file != current_flat_file:
            #     active_flat_file = current_flat_file
            #     del sciFac.flat
            #     sciFac.flat = fits.open(active_flat_file) if os.path.exists(active_flat_file) else None
            # sciFac.reset()
            # sciFac.add_image(im)
            # calim = sciFac.generate(threaded=False, save=False)

            #make figure
            if current_app.array_view_params['changed']:
                fig = go.Figure()
                fig.add_heatmap(z=im, showscale=False,
                                colorscale=[[0, "black"], [0.5, "white"], [0.5, "red"], [1, "red"]],
                                zmin=current_app.array_view_params['min_cts'],
                                zmax=current_app.array_view_params['max_cts'] * 2)
                fig.update_layout(dict(height=550, autosize=True,
                                       xaxis=dict(range=[0, 80], visible=False, ticks='', scaleanchor='y'),
                                       yaxis=dict(range=[0, 125], visible=False, ticks='')))
                fig.update_layout(margin=dict(l=0, r=0, b=0, t=0, pad=3))
                data = json.dumps({'id': 'dash',
                        'kind': 'full',
                        'data': json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder),
                        'time': datetime.datetime.fromtimestamp(time.time()).strftime("%m/%d/%Y %H:%M:%S.%f")[:-4]})
                current_app.array_view_params['changed'] = False
            else:
                data = json.dumps({'id': 'dash',
                                   'kind': 'partial',
                                   'data': json.dumps({'z': im}),
                                   'time': datetime.datetime.fromtimestamp(time.time()).strftime(
                                       "%m/%d/%Y %H:%M:%S.%f")[:-4]})

            toc = time.time()
            # print(toc-tic)

            yield f"event:dashplot\nretry:5\ndata:{data}\n\n"


    return current_app.response_class(_stream(), mimetype="text/event-stream", content_type='text/event-stream')


@bp.route('/send_obs_dict/<startstop>', methods=["POST"])
def send_obs_dict(startstop):
    # TODO: Modify this to be a 'sender' that either sends a start or stop obs dict.
    if request.method == "POST":
        target = request.values.get("name")
        if target == "---":
            target = ''
        obs_type = request.values.get("type").lower()
        duration = float(request.values.get("duration"))
        start = datetime.datetime.utcnow().timestamp()
        seq_i = int(request.values.get("seq_i"))
        seq_n = int(request.values.get("seq_n"))
        if obs_type != "abort":
            obs_dict = {'name': target, "type": obs_type,
                        'duration': duration, 'start': start,
                        'seq_i': seq_i, 'seq_n': seq_n}
        else:
            obs_dict = {'type': obs_type}

    log.debug(f"{startstop} sending photons")
    log.info(f"Observing command: {obs_dict}")

    if startstop == "start":
        log.info(f"Start observing target: {target}")
        current_app.redis.publish("command:observation-request", json.dumps(obs_dict), store=False)
        current_app.redis.store({"observing:target": target})
    else:
        log.info(f"Stop observing target: {target}")
        current_app.redis.publish("command:observation-request", json.dumps(obs_dict), store=False)
    return '', 204

@bp.route('/receive_obs_dict', methods=["GET"])
def receive_obs_dict():
    """
    Receives an obs_dict and passes it back to flask to handle appropriately
    TODO: Make sure that this triggers after starting/stopping observing has been successful
    N.B: if there's any issue with starting, one may just click 'stop', otherwise
    """
    @stream_with_context
    def _stream():
        while True:
            for k, v in current_app.redis.listen("command:observation-request"):
                log.debug(f"Heard {k}, {v}")
                msg = f"retry:5\ndata: {v}\n\n"
                yield msg

    return current_app.response_class(_stream(), mimetype='text/event-stream', content_type='text/event-stream')


# TODO: In command functions, import the proper command keys if appropriate
@bp.route('/update_laser_powers', methods=["POST"])
def update_laser_powers():
    msg_success = 0

    if request.method == "POST":
        wvl = json.loads(request.values.get("wvl"))
        power = json.loads(request.values.get("power"))
        if isinstance(wvl, list):
            new_powers = {w: min(100, max(int(p), 0)) for w, p in zip(wvl, power)}
        else:
            new_powers = {wvl: min(100, max(power, 0))}

    try:
        for k, v in new_powers.items():
            log.debug(f"Setting {k} nm laser to {v}% power")
            msg_success += current_app.redis.publish(f"command:device-settings:laserflipperduino:laserbox:{k}:power", v,
                                                     store=False)
    except RedisError as e:
        log.warning(f"Can't communicate with Redis Server! {e}")
        sys.exit(1)

    powers = {k: int(float(current_app.redis.read(f"device-settings:laserflipperduino:laserbox:{k}:power"))) for k in
              new_powers.keys()}

    resp = {'success': msg_success, 'powers': powers}

    return json.dumps(resp)


@bp.route('/flip_mirror/<position>', methods=["POST"])
def flip_mirror(position):
    msg_success = 0

    if position.lower() == "up":
        new_pos = "Up"
    else:
        new_pos = "Down"

    try:
        log.debug(f"Setting flip mirror to position: {new_pos}")
        msg_success += current_app.redis.publish("command:device-settings:laserflipperduino:flipper:position", new_pos,
                                                 store=False)
        log.info(f"Flip mirror set to position: {new_pos}")
    except RedisError as e:
        log.warning(f"Can't communicate with Redis Server! {e}")
        sys.exit(1)

    position = current_app.redis.read('device-settings:laserflipperduino:flipper:position')

    resp = {'success': msg_success, 'position': position}

    return json.dumps(resp)


@bp.route('/move_focus/<position>', methods=["POST"])
def move_focus(position):
    msg_success = 0
    if position == "home":
        log.debug("Sending command to home focus stage")
        msg_success += current_app.redis.publish('command:device-settings:focus:home', 'home', store=False)
    else:
        position = min(50, max(0, float(position)))  # Can only move between 0-50 mm
        log.debug(f"Command focus stage to move to {position}")
        msg_success += current_app.redis.publish('command:device-settings:focus:desired-position:mm', position,
                                                 store=False)

    position = current_app.redis.read('status:device:focus:position:mm')[1]

    resp = {'success': msg_success, 'position': position}

    return json.dumps(resp)


@bp.route('/change_filter/<filter>', methods=['POST'])
def change_filter(filter):
    filterno, filtername = filter.split(':')
    msg_success = 0

    FDATA = {k: f"{k}:{v}" for k, v in FILTERS.items()}

    try:
        log.debug(f"Setting filter mirror to position: {filterno} ({filtername})")
        msg_success += current_app.redis.publish('command:device-settings:filterwheel:position', filterno, store=False)
    except RedisError as e:
        log.warning(f"Can't communicate with Redis Server! {e}")
        sys.exit(1)

    filterpos = current_app.redis.read('device-settings:filterwheel:position')
    resp = {'success': msg_success, 'filter': FDATA[int(filterpos)]}

    return json.dumps(resp)


@bp.route('/update_array_viewer_params', methods=['POST'])
def update_array_viewer_params():

    if request.method == "POST":
        param = request.values.get("param")
        value = request.values.get("value")

    log.info(f"Updating array viewer parameter {param} to {value}")
    if param == "int_time":
        new_val = min(max(float(value), 0.01), 10.0)
    elif param == "min_cts":
        new_val = max(0, min(int(value), current_app.array_view_params['max_cts'] - 10))
    elif param == "max_cts":
        new_val = min(5000, max(int(value), current_app.array_view_params['min_cts'] + 10))
    current_app.array_view_params[param] = new_val
    current_app.array_view_params['changed'] = True
    resp = {'value': new_val}

    return json.dumps(resp)


@bp.route('/command_conex', methods=['POST'])
def command_conex():
    msg_success = 0

    if request.method == "POST":
        cmd = request.values.get("cmd")
        if cmd == "move":
            pos = request.values.get("position")
            x, y = pos.split(',')
            conex_cmd = "conex:move"
            send_dict = {'x': x, 'y': y}
        elif cmd == "dither":
            dith_info = json.loads(request.values.get("dither_info"))
            startx, starty = dith_info['start'].split(',')
            stopx, stopy = dith_info['stop'].split(',')
            conex_cmd = "conex:dither"
            send_dict = {'startx': float(startx), 'stopx': float(stopx),
                         'starty': float(starty), 'stopy': float(stopy),
                         'n': int(float(dith_info['n'])), 't': float(dith_info['t'])}
        elif cmd == "stop":
            conex_cmd = "conex:stop"
            send_dict = {}

    msg_success += current_app.redis.publish(conex_cmd, json.dumps(send_dict), store=False)
    log.debug(f"Commanding conex to {cmd}. Params: {send_dict}")

    return json.dumps({'success': msg_success})


@bp.route('/command_heatswtich/<to_position>', methods=['POST'])
def command_heatswitch(to_position):
    # TODO: Enable/disable heatswitch commands?
    to_position = to_position.lstrip('hs_')
    msg_success = 0

    log.info(f"Commanding heatswitch to {to_position}")

    if to_position in ('open', 'close'):
        hs_key = "command:device-settings:heatswitch:position"
    elif to_position == "stop":
        hs_key = "command:heatswitch:stop"
    else:
        log.warning(f"Trying to command the heatswitch to an unknown state!")

    msg_success += redis.publish(hs_key, to_position, store=False)

    return json.dumps({'success': msg_success})


@bp.route('/command_magnet/<cmd>/<at>', methods=['POST'])
def command_magnet(cmd, at):
    log.debug(f"Heard command {cmd} (at {at})")
    msg_success = 0
    now = time.time()

    if cmd == "start_cycle":
        magnet_command = "command:get-cold"
        at = "now"
    elif cmd == "abort_cycle":
        magnet_command = "command:abort-cooldown"
        at = "now"
    elif cmd == "schedule_cycle":
        magnet_command = "command:be-cold-at"
        at = datetime.datetime.strptime(at, '%Y-%m-%dT%H:%M').timestamp()
    elif cmd == "cancel_scheduled_cycle":
        magnet_command = "command:cancel-scheduled-cycle"
        at = "now"

    msg_success += current_app.redis.publish(magnet_command, at, store=False)
    scheduled = True if (current_app.redis.read('device-settings:magnet:cooldown-scheduled') == "yes") else False

    if cmd == "schedule_cycle":
        msg_success = 1 if scheduled else 0

    return json.dumps({'success': msg_success, 'scheduled': scheduled})


@bp.route('/notifications')
@login_required
def notifications():
    since = request.args.get('since', 0.0, type=float)
    notifications = current_user.notifications.filter(
        Notification.timestamp > since).order_by(Notification.timestamp.asc())
    return jsonify([{'name': n.name, 'data': n.get_data(), 'timestamp': n.timestamp} for n in notifications])


def spin_up_packetmaster(dashcfgfile, initializing_gui=False):
    # TODO: This is where we should spin up packetmaster/create a dither log. This allows us to run it on initialization
    #  of the gui or when we update the dashboard cfg
    #  NB This should only create a new dither log on the initialization of the gui, not on updating the file
    pass
