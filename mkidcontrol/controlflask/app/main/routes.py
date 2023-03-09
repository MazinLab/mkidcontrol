import sys
import os
import shutil
import subprocess
import threading
from datetime import datetime

import numpy as np
import plotly.graph_objects as go
import astropy.units as u
from astropy.io import fits
from astropy.coordinates import Angle

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
import json
import glob
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

from mkidcontrol.agents.xkid.observingAgent import OBSERVING_EVENT_KEY, DATA_DIR_KEY
from mkidcontrol.agents.xkid.conexAgent import CONEX_REF_X_KEY, CONEX_REF_Y_KEY, PIXEL_REF_X_KEY, PIXEL_REF_Y_KEY

# TODO: ObsLog, ditherlog, dashboardlog

# TODO: Add redis key capturing whether we are observing or not!

# TODO: With the GUI it needs to pass the 'at a glance test' -> the user should be able to tell whats going on from a simple look
#  Think "green for good, red for error", good compartmentalization (spacing on page and similar things go together), less clutter

# TODO: Command handling

# TODO: Form submission only changes changed values (e.g. don't change Curve No. = 8 -> Curve No. = 8)

# TODO: MUST TEST FOR CONCURRENCY ISSUES (Controlling the instrument from multiple tabs, does it work, does it stay in
#  sync?), do pages need to reload to show status updates or do they just update?

# TODO: Work with auto-discovery where possible (for keys/programs/etc)


redis.setup_redis(ts_keys=REDIS_TS_KEYS)

log = setup_logging('controlDirector')


def guess_language(x):
    return 'en'


@bp.before_app_request
def before_request():
    if current_user.is_authenticated:
        current_user.last_seen = datetime.utcnow()
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
    TODO: Support message flashing
    """
    try:
        current_app.redis.read(FLASK_KEYS)
    except RedisError:
        return redirect(url_for('main.redis_error_page'))
    except KeyError:
        flash(f"Redis keys are missing!")

    from mkidcontrol.commands import Laserbox, Filterwheel, Focus

    magnetform = MagnetCycleForm()
    hsform = HeatSwitchForm()
    laserbox = LaserBoxForm(**vars(Laserbox(redis)))
    fw = FilterWheelForm(**vars(Filterwheel(redis)))
    focus = FocusForm(**vars(Focus(redis)))
    obs = ObsControlForm()
    conex = ConexForm()

    last_observing_event = current_app.redis.read(OBSERVING_EVENT_KEY, decode_json=True)
    cooldown_scheduled = True if (
                current_app.redis.read('device-settings:magnet:cooldown-scheduled').lower() == "yes") else False
    if cooldown_scheduled:
        cooldown_time = float(current_app.redis.read('device-settings:magnet:cooldown-scheduled:timestamp'))
        cooldown_time = datetime.fromtimestamp(cooldown_time).strptime('%Y-%m-%dT%H:%M')
    else:
        cooldown_time = (datetime.today().date() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')

    form = FlaskForm()

    sensor_fig = multi_sensor_fig(FLASK_CHART_KEYS.keys())
    array_fig = initialize_array_figure(current_app.array_view_params)
    pix_lightcurve = pixel_lightcurve()

    return render_template('index.html', last_observing_event=last_observing_event, magnetform=magnetform,
                           hsform=hsform, fw=fw,
                           focus=focus, form=form, laserbox=laserbox, obs=obs, conex=conex, sensor_fig=sensor_fig,
                           array_fig=array_fig, cooldown_scheduled=cooldown_scheduled, cooldown_time=cooldown_time,
                           pix_lightcurve=pix_lightcurve, sensorkeys=list(FLASK_CHART_KEYS.values()))


@bp.route('/conex_normalization', methods=['GET', 'POST'])
def conex_normalization():
    """
    """

    refs = current_app.redis.read([CONEX_REF_X_KEY, CONEX_REF_Y_KEY, PIXEL_REF_X_KEY, PIXEL_REF_Y_KEY])
    refs = {k.lstrip("instrument:").replace("-", "_"): v for k, v in refs.items()}

    conex = ConexForm()
    norm = ConexNormalizationForm(**refs)
    obs = ObsControlForm()

    array_fig = initialize_array_figure(current_app.array_view_params)

    return render_template('conex_normalization.html', conex=conex, array_fig=array_fig, norm=norm, obs=obs)


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
    # TODO: Fix Job.fetch(id, ...), id='email-logs' does not work
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


@bp.route('/data_paths', methods=['GET', 'POST'])
def data_paths():
    # TODO: Turn new night & configuration into one page once further stabilized & tested
    # TODO: Add "you must restart the gui if you change the beammap" message

    from mkidcontrol.commands import Paths

    paths = Paths(current_app.redis)

    ymls_to_copy = ['dashboard.yml', 'hightemplar.yml', 'roach.yml']
    subdirs = [paths.bin_folder_name, paths.config_folder_name, paths.config_folder_name+'/logs',
               'data', 'data/phasesnaps', paths.fits_folder_name, paths.logs_folder_name]
    roaches = ['112', '114', '115', '116', '117', '118', '119', '120', '121', '122']  # TODO : make discoverable via yaml

    if request.method == "POST":
        if "new_night" in request.form.keys():
            current_dir = redis.read('paths:data-dir')
            base_dir = current_app.base_dir

            newdate = (datetime.utcnow()+datetime.timedelta(hours=12)).strftime("%Y%m%d")
            new_night_dir = os.path.join(base_dir, f"ut{newdate}")
            try:
                os.mkdir(new_night_dir)
            except FileExistsError:
                log.info(f"The directory for the new night ({new_night_dir} already exists!")

            for subdir in subdirs:
                try:
                    os.mkdir(os.path.join(new_night_dir, subdir))
                except FileExistsError:
                    log.debug(f"The subdirectory {subdir} already exists in {new_night_dir}!")

            for roach in roaches:
                try:
                    os.mkdir(os.path.join(new_night_dir, 'data/phasesnaps', roach))
                except FileExistsError:
                    log.info(f"The phasesnap directory for roach {roach} already exists in {new_night_dir}!")

                try:
                    shutil.copy(os.path.join(new_night_dir, 'data/phasesnaps', roach, 'filter_solution_coefficients.npz'), os.path.join(new_night_dir, 'data/phasesnaps', roach))
                    shutil.copy(os.path.join(new_night_dir, 'data/phasesnaps', roach, 'filter_solution.p'), os.path.join(new_night_dir, 'data/phasesnaps', roach))
                except:
                    log.error(f"Unable to copy {os.path.join(new_night_dir, 'data/phasesnaps', roach)} to {os.path.join(new_night_dir, 'data/phasesnaps', roach)}")

            for yml in ymls_to_copy:
                try:
                    shutil.copy(os.path.join(current_dir, paths.config_folder_name, yml), os.path.join(new_night_dir, paths.config_folder_name))
                except:
                    log.error(f"Unable to copy {os.path.join(current_dir, paths.config_folder_name, yml)} to {os.path.join(new_night_dir, paths.config_folder_name)}")

            try:
                redis.store({'paths:data-dir': new_night_dir})
                redis.store({'gen2-dashboard-yaml': os.path.join(new_night_dir, paths.config_folder_name, 'dashboard.yml')})
            except RedisError as e:
                log.warning(f"Error communicating with redis! Error: {e}")
        elif "update" in request.form.keys():
            # TODO
            pass
        else:
            # Should not be able to get here
            log.info("Unknown submit action taken for data paths! Taking no action")
            pass

    paths = Paths(current_app.redis)
    pathform = DataPathForm(**vars(paths))

    return render_template('data_paths.html', title=_('Configuration Paths'), pathform=pathform)


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

            last_bin_file = max(glob.glob(os.path.join(current_app.redis.read(DATA_DIR_KEY), current_app.redis.read("paths:bin-folder-name"), '*.bin')), key=os.path.getctime)
            last_bin_file = last_bin_file.split("/")[-1] + f" ({int(os.stat(last_bin_file).st_size/(1024*1024))} MB)"

            x.update({'unix-timestamp': int(datetime.utcnow().timestamp())})
            x.update({'utc-timestamp': datetime.utcnow().strftime("%m/%d/%Y %H:%M:%S")})
            x.update({'latest-bin-file': last_bin_file})

            x.update(s)
            x["tcs:ra"] = degrees_to_sexigesimal(x['tcs:ra'])
            x["tcs:dec"] = degrees_to_sexigesimal(x['tcs:dec'])

            x = json.dumps(x)
            msg = f"retry:5\ndata: {x}\n\n"
            yield msg

    return current_app.response_class(_stream(), mimetype='text/event-stream', content_type='text/event-stream')


def degrees_to_sexigesimal(angle):
    # Convert angle in degrees to sexigesimal
    ang = f"{angle} degrees"
    ang = Angle(ang).to_string(unit=u.degree, sep=":")
    if ang[0] in ['-', '+']:
        ang = ang[:12]
    else:
        ang = ang[:11]
    return ang


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
        fig.update_layout(dict(title=f"Pixel ({pix_x}, {pix_y})", xaxis=dict(tickangle=45, nticks=5)))
    fig = json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)
    return fig


def create_fig(name):
    since = None
    first_tval = int((datetime.now() - timedelta(hours=5)).timestamp() * 1000) if not since else since
    timestream = np.array(current_app.redis.mkr_range(FLASK_CHART_KEYS[name], f"{first_tval}"))
    if timestream[0][0] is not None:
        times = [datetime.fromtimestamp(t / 1000).strftime("%m/%d/%Y %H:%M:%S") for t in timestream[:, 0]]
        vals = list(timestream[:, 1])
    else:
        times = [None]
        vals = [None]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=times, y=vals, mode='lines', name=f"{name}"))
    fig.update_layout(dict(title=f"{name}", xaxis=dict(tickangle=45, nticks=3)))
    fig = json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)
    return fig


def multi_sensor_fig(titles):
    since = None
    first_tval = int((datetime.now() - timedelta(hours=0.5)).timestamp() * 1000) if not since else since
    keys = [FLASK_CHART_KEYS[title] for title in titles]

    timestreams = [np.array(current_app.redis.mkr_range(key, f"{first_tval}")) for key in keys]
    times = []
    for ts in timestreams:
        if ts[0][0] is not None:
            times.append([datetime.fromtimestamp(t / 1000).strftime("%m/%d/%Y %H:%M:%S") for t in ts[:, 0]])
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
        dict(xaxis=dict(tickangle=45, nticks=5),
             updatemenus=list([dict(buttons=update_menus, x=0.01, xanchor='left', y=1.1, yanchor='top')])))
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
    @stream_with_context
    def _stream():
        event = threading.Event()
        current_app.image_events.add(event)
        new=True
        try:
            while True:
                event.wait()
                event.clear()
                im = current_app.latest_image
                params = current_app.array_view_params.copy()
                current_app.array_view_params['changed'] = False
                update = {'id': 'dash', 'time': datetime.utcnow().strftime("%m/%d/%Y %H:%M:%S")[:-4]}
                # make figure
                if params['changed'] or new:
                    new=False
                    log.info('Params changed, regenerating full plot')
                    fig = go.Figure()
                    fig.add_heatmap(z=im, showscale=False,
                                    colorscale=[[0, "black"], [0.5, "white"], [0.5, "red"], [1, "red"]],
                                    zmin=params['min_cts'], zmax=params['max_cts'] * 2)
                    fig.update_layout(dict(height=550, autosize=True,
                                           xaxis=dict(range=[0, 80], visible=False, ticks='', scaleanchor='y'),
                                           yaxis=dict(range=[0, 125], visible=False, ticks='')))
                    fig.update_layout(margin=dict(l=0, r=0, b=0, t=0, pad=3))
                    update['kind'] = 'full'
                    update['data'] = json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)
                else:
                    update['kind'] = 'partial'
                    update['data'] = json.dumps({'z': im}, cls=plotly.utils.PlotlyJSONEncoder)

                data = json.dumps(update)
                yield f"event:dashplot\nretry:5\ndata:{data}\n\n"
        finally:
            current_app.image_events.discard(event)

    return current_app.response_class(_stream(), mimetype="text/event-stream", content_type='text/event-stream')


@bp.route('/send_obs_dict/<startstop>', methods=["POST"])
def send_obs_dict(startstop):
    # TODO: Modify this to be a 'sender' that either sends a start or stop obs dict.
    if request.method == "POST":
        name = request.values.get("name")
        if name == "":
            log.error(f"Cannot start an observation without a name!!")
        obs_type = request.values.get("type").lower()
        duration = float(request.values.get("duration"))
        start = datetime.utcnow().timestamp()
        seq_i = int(request.values.get("seq_i"))
        seq_n = int(request.values.get("seq_n"))
        if obs_type != "abort":
            obs_dict = {'name': name, "type": obs_type,
                        'duration': duration, 'start': start,
                        'seq_i': seq_i, 'seq_n': seq_n}
        else:
            obs_dict = {'type': obs_type}

    if name == "":
        log.error(f"Cannot start an observation without a name!!")
    else:
        log.debug(f"{startstop} sending photons")
        log.info(f"Observing command: {obs_dict}")

        if startstop == "start":
            log.info(f"Start observing: {name}")
            current_app.redis.publish("command:observation-request", json.dumps(obs_dict), store=False)
            # current_app.redis.store({"observing:target": name})
        else:
            log.info(f"Stop observing: {name}")
            current_app.redis.publish("command:observation-request", json.dumps(obs_dict), store=False)
    return '', 204


@bp.route('/report_obs_status', methods=["GET"])
def report_obs_status():
    """
    Receives an obs_dict and passes it back to flask to handle appropriately
    N.B: if there's any issue with starting, one may just click 'stop', otherwise
    """

    @stream_with_context
    def _stream():
        while True:
            for key, val in redis.listen(OBSERVING_EVENT_KEY):
                log.debug(f"Observating event! {val}")
                msg = f"retry:5\ndata: {val}\n\n"
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

    time.sleep(.5)
    powers = {k: int(float(current_app.redis.read(f"device-settings:laserflipperduino:laserbox:{k}:power"))) for k in
              new_powers.keys()}

    resp = {'success': msg_success, 'powers': powers}

    return json.dumps(resp)


@bp.route('/flip_mirror', methods=["POST"])
def flip_mirror():
    msg_success = 0

    if request.method == "POST":
        position = request.values.get("position")

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


@bp.route('/move_focus', methods=["POST"])
def move_focus():
    msg_success = 0
    if request.method == "POST":
        position = request.values.get("position")

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


@bp.route('/change_filter', methods=['POST'])
def change_filter():
    if request.method == "POST":
        filter = request.values.get("filter")

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
    conex_cmd = ""

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
            send_dict = {'name': dith_info['name'],
                         'startx': float(startx), 'stopx': float(stopx),
                         'starty': float(starty), 'stopy': float(stopy),
                         'n': int(float(dith_info['n'])), 't': float(dith_info['t'])}
        elif cmd == "stop":
            conex_cmd = "conex:stop"
            send_dict = {}
        elif cmd == "normalize":
            conex_ref_x = request.values.get("conex_ref_x")
            conex_ref_y = request.values.get("conex_ref_y")
            pixel_ref_x = request.values.get("pixel_ref_x")
            pixel_ref_y = request.values.get("pixel_ref_y")
            update_dict = {CONEX_REF_X_KEY: conex_ref_x, CONEX_REF_Y_KEY: conex_ref_y,
                           PIXEL_REF_X_KEY: pixel_ref_x, PIXEL_REF_Y_KEY: pixel_ref_y}
            current_app.redis.store(update_dict)
            msg_success += 1

    if conex_cmd:
        msg_success += current_app.redis.publish(f"command:{conex_cmd}", json.dumps(send_dict), store=False)
        log.debug(f"Commanding conex to {cmd}. Params: {send_dict}")

    return json.dumps({'success': msg_success})


@bp.route('/command_heatswtich', methods=['POST'])
def command_heatswitch():
    if request.method == "POST":
        to_position = request.values.get("to_position")
    to_position = to_position.lstrip('hs_')
    msg_success = 0

    log.info(f"Commanding heatswitch to {to_position}")

    if to_position in ('open', 'close'):
        hs_key = "command:device-settings:heatswitch:position"
    elif to_position == "stop":
        hs_key = "command:heatswitch:stop"
    else:
        log.warning(f"Trying to command the heatswitch to an unknown state!")

    msg_success += redis.publish(hs_key, to_position.capitalize(), store=False)

    return json.dumps({'success': msg_success})


@bp.route('/command_magnet', methods=['POST'])
def command_magnet():
    if request.method == "POST":
        cmd = request.values.get("cmd")
        at = request.values.get("at")

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
        at = datetime.strptime(at, '%Y-%m-%dT%H:%M').timestamp()
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
