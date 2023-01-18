import subprocess
from datetime import datetime
import plotly.graph_objects as go

import mkidcontrol.mkidredis
from mkidcontrol.mkidredis import RedisError
from mkidcontrol.util import setup_logging
from mkidcontrol.util import get_service as mkidcontrol_service
from mkidcontrol.util import get_services as mkidcontrol_services

from flask import render_template, flash, redirect, url_for, g, request, \
    jsonify, current_app, Response, copy_current_request_context
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

from mkidcontrol.controlflask.config import Config
import mkidcontrol.mkidredis as redis
from mkidcontrol.commands import COMMAND_DICT, SimCommand
from mkidcontrol.config import REDIS_TS_KEYS as TS_KEYS

from mkidcontrol.controlflask.app.main.forms import *

# TODO: Make sure columns/divs support resizing

# TODO: With the GUI it needs to pass the 'at a glance test' -> the user should be able to tell whats going on from a simple look
#  Think "green for good, red for error", good compartmentalization (spacing on page and similar things go together), less clutter

# TODO: Make sure 'submit' keys don't need to reload pages, just submit values and update accordingly

# TODO: Form submission only changes changed values (e.g. don't change Curve No. = 8 -> Curve No. = 8)

# TODO: MUST TEST FOR CONCURRENCY ISSUES (Controlling the instrument from multiple tabs, does it work, does it stay in
#  sync?), do pages need to reload to show status updates or do they just update?

# TODO: Work with auto-discovery where possible (for keys/programs/etc)

# TODO: Rework statuses, no need for 'ok'/'enabled', just flash an error along the top of screen

# TODO: Where is TCS data on the home screen?

# TODO: Move all these key definitions to config.py where all the other redis db and key stuff lives
CHART_KEYS = {'Device T': 'status:temps:device-stage:temp',
              'Device R': 'status:temps:device-stage:resistance',
              '1k Stage T': 'status:temps:1k-stage:temp',
              '1k Stage R': 'status:temps:1k-stage:resistance',
              '3k Stage T': 'status:temps:3k-stage:temp',
              '3k Stage V': 'status:temps:3k-stage:voltage',
              '50k Stage T': 'status:temps:50k-stage:temp',
              '50k Stage V': 'status:temps:50k-stage:voltage',
              'Magnet I': 'status:magnet:current',
              'Magnet Field': 'status:magnet:field',
              'LS625 Output V': 'status:device:ls625:output-voltage'}


# TODO: status:*:status is not super useful, consider renaming
# TODO: Keys would be useful where the rest of the keys are defined in mkidcontrol.config
KEYS = list(COMMAND_DICT.keys()) + list(TS_KEYS) + ['status:device:heatswitch:position',
                                                    'status:device:ls336:status',
                                                    'status:device:ls372:status',
                                                    'status:device:ls625:status',
                                                    'status:device:heatswitch:status']

# DASHDATA = np.load('/mkidcontrol/mkidcontrol/frontend/dashboard_placeholder.npy')

redis.setup_redis(ts_keys=TS_KEYS)

log = setup_logging('controlDirector')


def guess_language(x):
    return 'en'


@bp.before_app_request
def before_request():
    if current_user.is_authenticated:
        current_user.last_seen = datetime.datetime.utcnow()
        db.session.commit()
    g.locale = str(get_locale())
    if current_app.redis:
        g.redis = current_app.redis
    else:
        g.redis = mkidcontrol.mkidredis.setup_redis(ts_keys=TS_KEYS)


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
    TODO: Make robust to redis not being up and running
    TODO: Handle 'post' requests
    """
    try:
        redis.read(KEYS)
    except RedisError:
        return redirect(url_for('main.redis_error_page'))
    except KeyError:
        return flash(f"Redis keys are missing!")

    # from mkidcontrol.controlflask.app.main.forms import MagnetCycleForm, ScheduleForm, HeatSwitchForm2
    magnetform = MagnetCycleForm()
    schedule = ScheduleForm()
    hsform = HeatSwitchForm2()
    obsform = ObsControlForm()

    form = FlaskForm()
    if request.method == 'POST':
        print(request.form)

    sensor_fig = multi_sensor_fig(CHART_KEYS.keys())
    array_fig = view_array_data()
    pix_lightcurve = pixel_lightcurve()

    return render_template('index.html', magnetform=magnetform, schedule=schedule,
                           hsform=hsform, obsform=obsform, form=form,
                           sensor_fig=sensor_fig, array_fig=array_fig,
                           pix_lightcurve=pix_lightcurve,
                           sensorkeys=list(CHART_KEYS.values()))


@bp.route('/other_plots', methods=['GET'])
def other_plots():
    """
    Flask endpoint for 'other plots'. This page has ALL sensor plots in one place for convenience (in contrast to index,
    which only has one at a time).
    """

    form = FlaskForm()

    plots = [create_fig(title) for title in CHART_KEYS.keys()]

    ids = ['device_t', 'device_r',
           'onek_t', 'onek_r',
           'threek_t', 'threek_v',
           'fiftyk_t', 'fiftyk_v',
           'magnet_i', 'magnet_f',
           'ls625_ov']

    return render_template('other_plots.html', title=_('Other Plots'), form=form,
                           plots=plots, ids=ids, sensorkeys=list(CHART_KEYS.values()))


@bp.route('/settings', methods=['GET', 'POST'])
def settings():
    """
    Flask endpoint for settings page. Handles setting changes for housekeeping instruments
    TODO: Readout settings (when we have a readout)
    """
    if request.method == 'POST':
        return handle_validation(request, submission=True)

    return render_template('settings.html', title=_('Settings'))


@bp.route('/log_viewer', methods=['GET', 'POST'])
def log_viewer():
    """
    Flask endpoint for log viewer. This page is solely for observing the journalctl output from each agent.
    # TODO: Update html
    """
    form = FlaskForm()
    return render_template('log_viewer.html', title=_('Log Viewer'), form=form)


@bp.route('/heater/<device>/<channel>', methods=['GET', 'POST'])
def heater(device, channel):
    from ....commands import LakeShoreCommand

    if request.method == 'POST':
        for key in request.form.keys():
            print(f"{key} : {request.form.get(key)}")
            try:
                x = LakeShoreCommand(f"device-settings:{device}:heater-channel-{request.form.get('channel').lower()}:{key.replace('_','-')}", request.form.get(key))
                log.info(f"Sending command:{x.setting}' -> {x.value} ")
                redis.publish(f"command:{x.setting}", x.value)
                log.info(f"Command sent successfully")
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


@bp.route('/thermometry/<device>/<channel>', methods=['GET', 'POST'])
def thermometry(device, channel):
    try:
        title = redis.read(f'device-settings:{device}:input-channel-{channel.lower()}:name')
    except:
        return redirect(url_for('main.page_not_found'))

    from ....commands import LakeShoreCommand

    if request.method == 'POST':
        print(f"Form: {request.form}")
        for key in request.form.keys():
            print(f"{key} : {request.form.get(key)}")
            try:
                x = LakeShoreCommand(f"device-settings:{device}:input-channel-{request.form.get('channel').lower()}:{key.replace('_','-')}", request.form.get(key))
                log.info(f"Sending command:{x.setting}' -> {x.value} ")
                redis.publish(f"command:{x.setting}", x.value)
                log.info(f"Command sent successfully")
            except ValueError as e:
                log.warning(f"Value error: {e} in parsing commands")
                log.debug(f"Unrecognized field to send as command: {key}")
            time.sleep(.15)

    # TODO: Turn all of this if/else into a single 'thermometry' form
    if device == 'ls336':
        from mkidcontrol.controlflask.app.main.forms import RTDForm, DiodeForm, DisabledInputForm
        from ....commands import LS336InputSensor

        sensor = LS336InputSensor(channel=channel, redis=redis)
        if sensor.sensor_type == "NTC RTD":
            form = RTDForm(**vars(sensor))
        elif sensor.sensor_type == "Diode":
            form = DiodeForm(**vars(sensor))
        elif sensor.sensor_type == "Disabled":
            form = DisabledInputForm(**vars(sensor))
    elif device == 'ls372':
        from mkidcontrol.controlflask.app.main.forms import ControlSensorForm, InputSensorForm
        from ....commands import LS372InputSensor, ALLOWED_372_INPUT_CHANNELS
        sensor = LS372InputSensor(channel=channel, redis=redis)
        # TODO: Enable/disable
        if channel == "A":
            form = ControlSensorForm(**vars(sensor))
        elif channel in ALLOWED_372_INPUT_CHANNELS[1:]:
            form = InputSensorForm(**vars(sensor))
    else:
        return redirect(url_for('main.page_not_found'))

    return render_template('thermometry.html', title=_(f"{title} Thermometer"), form=form)


@bp.route('/ls625', methods=['POST', 'GET'])
def ls625():
    # from mkidcontrol.controlflask.app.main.forms import Lakeshore625ControlForm
    from ....commands import LakeShoreCommand, LS625MagnetSettings

    ls625settings = LS625MagnetSettings(redis)

    if request.method == 'POST':
        for key in request.form.keys():
            print(f"{key} : {request.form.get(key)}")
            try:
                x = LakeShoreCommand(f"device-settings:ls625:{key.replace('_','-')}", request.form.get(key), limit_vals=ls625settings.limits)
                log.info(f"Sending command:{x.setting}' -> {x.value} ")
                redis.publish(f"command:{x.setting}", x.value)
                log.info(f"Command sent successfully")
            except ValueError as e:
                log.warning(f"Value error: {e} in parsing commands")
                log.debug(f"Unrecognized field to send as command: {key}")
            time.sleep(0.15)

    form = Lakeshore625ControlForm(**vars(ls625settings))

    return render_template('ls625.html', title=_("Magnet Power Supply Control"), form=form)


@bp.route('/heatswitch/<mode>', methods=['POST', 'GET'])
def heatswitch(mode):
    # from mkidcontrol.controlflask.app.main.forms import HeatSwitchForm, HeatSwitchEngineeringModeForm
    if request.method == "POST":
        print(f"Form: {request.form}")
        for key in request.form.keys():
            print(f"{key} : {request.form.get(key)}")

    form = HeatSwitchForm()

    return render_template('heatswitch.html', title=_('Heat Switch'), form=form)


@bp.route('/services')
def services():
    services = mkidcontrol_services()
    try:
        job = Job.fetch('email-logs', connection=g.redis.redis)
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
        redis.setup_redis(ts_keys=TS_KEYS)
        return jsonify({'success': True})
    else:
        return bad_request('Invalid shutdown command')


@bp.route('/test_page', methods=['GET', 'POST'])
def test_page():

    from mkidcontrol.controlflask.app.main.forms import MagnetCycleSettingsForm
    if request.method == "POST":
        print(request.form)
    form = MagnetCycleSettingsForm()

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
    def _stream():
        while True:
            time.sleep(.75)
            x = redis.read(KEYS)
            y = mkidcontrol_services().items()
            s = {}
            for k,v in y:
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
        fig.update_layout(title=f"Pixel ({pix_x}, {pix_y})")
    fig = json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)
    return fig


def create_fig(name):
    since = None
    first_tval = int((datetime.datetime.now() - timedelta(hours=5)).timestamp() * 1000) if not since else since
    timestream = np.array(redis.mkr_range(CHART_KEYS[name], f"{first_tval}"))
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
    keys = [CHART_KEYS[title] for title in titles]

    timestreams = [np.array(redis.mkr_range(key, f"{first_tval}")) for key in keys]
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
    fig.update_layout(dict(updatemenus=list([dict(buttons=update_menus, x=0.01, xanchor='left', y=1.1, yanchor='top')])))
    fig = json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)
    return fig


def view_array_data():
    """
    Placeholding function to grab a frame from a (hard-coded, previously made) temporal drizzle to display as the
    'device view' on the homepage of the flask application.
    """
    x = np.zeros((125, 80))
    noise = 5 * np.random.randn(125, 80)
    y = x + noise
    fig = go.Figure()
    fig.add_heatmap(z=y.tolist(), showscale=False)
    fig.update_layout(dict(height=550, autosize=True, xaxis=dict(visible=False, ticks='', scaleanchor='y'), yaxis=dict(visible=False, ticks='')))
    fig.update_layout(margin=dict(l=0, r=0, b=0, t=0, pad=3))
    fig = json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)
    return fig


@bp.route('/dashplot', methods=["GET"])
def dashplot():
    """
    TODO: Update appropriately following 'plot_data()' function from cloudflask
    """
    def _stream():
        while True:
            figdata = view_array_data()
            t = time.time()
            data = {'id':'dash', 'kind':'full', 'data':figdata, 'time':datetime.datetime.fromtimestamp(t).strftime("%m/$d/$Y %H:%M:%S.%f")[:-4]}
            yield f"event:dashplot\nretry:5\ndata: {json.dumps(data)}\n\n"
            time.sleep(1)  # TODO: Allow changed timesteps

    return current_app.response_class(_stream(), mimetype="text/event-stream", content_type='text/event-stream')


def parse_schedule_cooldown(schedule_time):
    """
    Takes a string input from the schedule cooldown field and parses it to determine if it is in a proper format to be
    used as a time for scheduling a cooldown.
    Returns a timestamp in seconds (to send to the SIM960 agent for scheduling), a datetime object (for reporting to
    flask page), and time until the desired cold time in seconds (to check for it being allowable)
    """
    pass

def handle_validation(req, submission=False):
    id = req.form.get('id')
    field_info = FIELD_KEYS[id]

    key = field_info['key']
    value = req.form.get('data')

    field_type = field_info['type']
    prefix_cmd = field_info['prefix']

    log.info(f"For field {id} (key: {key}), changing value to {value} with {field_type} methods.")
    if field_type in ('sim921', 'sim960', 'heatswitch', 'magnet'):
        try:
            s = SimCommand(key, value)
            is_legal = [True, '\u2713']
            if submission:
                if prefix_cmd:
                    log.debug(f"Sending command:{key} -> {value}")
                    redis.publish(f"command:{key}", value, store=False)
                else:
                    log.debug(f"Sending {key} -> {value}")
                    redis.publish(key, value)
        except ValueError:
            is_legal = [False, '\u2717']
        return jsonify({'cycle': False, 'key': key, 'value': value, 'legal': is_legal})
    elif field_type == 'cycle':
        if field_info['schedule']:
            try:
                x = parse_schedule_cooldown(value)
                soak_current = float(redis.read(SOAK_CURRENT_KEY))
                soak_time = float(redis.read(SOAK_TIME_KEY))
                ramp_rate = float(redis.read(RAMP_SLOPE_KEY))
                deramp_rate = float(redis.read(DERAMP_SLOPE_KEY))
                time_to_cool = ((soak_current - 0) / ramp_rate) + soak_time + ((0 - soak_current) / deramp_rate)
                if submission:
                    log.debug(f"{key} -> {value}, {x[0]}")
                    redis.publish(key, x[0], store=False)
                if x[2] >= time_to_cool:
                    return jsonify({'cycle': True, 'key': 'command:be-cold-at', 'value': datetime.datetime.strftime(x[1], "%m/%d/%y %H:%M:%S"), 'legal': [True, '\u2713']})
                else:
                    return jsonify({'cycle': True, 'key': 'command:be-cold-at', 'value': datetime.datetime.strftime(x[1], "%m/%d/%y %H:%M:%S"), 'legal': [False, '\u2717']})
            except Exception as e:
                return jsonify({'cycle': True, 'key': 'command:be-cold-at', 'value': value, 'legal': [False, '\u2717']})
        else:
            if submission:
                log.debug(f"{key} at {time.time()}")
                redis.publish(key, f"{time.time()}", store=False)
            return jsonify({'mag': True, 'key': key, 'value': time.strftime("%m/%d/%y %H:%M:%S"), 'legal': [True, '\u2713']})
    else:
        log.critical(f"Field type '{field_type}' not implemented!")


@bp.route('/notifications')
@login_required
def notifications():
    since = request.args.get('since', 0.0, type=float)
    notifications = current_user.notifications.filter(
        Notification.timestamp > since).order_by(Notification.timestamp.asc())
    return jsonify([{'name': n.name, 'data': n.get_data(), 'timestamp': n.timestamp} for n in notifications])


# ----- v Below == Cloudlight Stuff v -----

#
# @bp.route('/', methods=['GET', 'POST'])
# @bp.route('/index', methods=['GET', 'POST'])
# def index():
#     modes = tuple((key, e.name) for key, e in cloudlight.fadecandy.EFFECTS.items())
#     if request.method == 'POST':
#         mode = request.form['mode_key']
#         f = cloudlight.fadecandy.ModeFormV2(mode)
#         if mode == 'off':
#             g.redis.store(f'lamp:mode', mode)
#             g.redis.store(f'lamp:off:settings', EFFECTS['off'].defaults)
#         elif not f.validate_on_submit():
#             current_app.logger.warning(f'Validation of {f} failed.')
#             print(f)
#             print(f.errors)
#             print(f.data)
#             print('request form')
#             print(request.form)
#         else:
#
#             settings = f.settings.data
#             if f.schedule_data.schedule.data or f.schedule_data.clear.data:
#                 current_app.logger.debug('Clearing Scheduled lamp event')
#                 canceled = 'schedule' in current_app.scheduler
#                 current_app.scheduler.cancel('schedule')
#                 if not f.schedule_data.clear.data:
#                     current_app.logger.debug(f'Scheduling {mode} at {f.schedule_data.at.data}. '
#                                              f'repeat={f.schedule_data.repeat.data}')
#                     current_app.scheduler.schedule(f.schedule_data.at.data.astimezone(pytz.utc),
#                                                    repeat=0 if not f.schedule_data.repeat.data else None,
#                                                    interval=24 * 3600, id='schedule',
#                                                    func=f"cloudlight.cloudflask.app.tasks.lamp_to_mode", args=(mode,),
#                                                    kwargs={'mode_settings': settings,
#                                                            'mute': f.mute.data if 'mute' in f else None})
#                     date = f.schedule_data.at.data.strftime('%I:%M %p on %m/%d/%Y' if not f.schedule_data.repeat.data
#                                                             else 'every day at %I:%M %p')
#                     flashmsg = f'Effect {EFFECTS[mode].name} scheduled for {date}'
#                     flash(flashmsg + (' (replaced previous event).' if canceled else '.'))
#                 elif canceled:
#                     flash('Scheduled effect canceled.')
#             else:
#                 if f.reset.data:  # reset the effect to the defaults
#                     g.redis.store(f'lamp:{mode}:settings', EFFECTS[mode].defaults)
#                 else:  # f.save or f.enable either way update
#                     g.redis.store(f'lamp:{mode}:settings', settings)
#
#                 if g.redis.read('lamp:mode') == mode:
#                     g.redis.store(f'lamp:settings', True, publish_only=True)
#
#                 if 'mute' in f:
#                     g.redis.store('player:muted', f.mute.data)
#
#                 if f.submit.data:
#                     canceled = 'sleep_timer' in current_app.scheduler
#                     current_app.scheduler.cancel('sleep_timer')
#                     if f.sleep_timer.data:
#                         current_app.logger.debug(f'Scheduling sleep timer to turn off in {f.sleep_timer.data} minutes.')
#                         current_app.scheduler.enqueue_in(timedelta(minutes=f.sleep_timer.data),
#                                                          f"cloudlight.cloudflask.app.tasks.lamp_to_mode",
#                                                          'off', job_id='sleep_timer')
#                     if f.sleep_timer.data:
#                         if canceled:
#                             flash('Will now turn off in {f.sleep_timer.data:.0f} minutes.')
#                         else:
#                             flash(f'{EFFECTS[mode].name} will fade out in {f.sleep_timer.data:.0f} minutes.')
#                     elif canceled:
#                         flash('Sleep timer canceled.')
#
#                     g.redis.store(f'lamp:mode', mode)
#             settings = g.redis.read(f'lamp:{mode}:settings')
#
#             morning = datetime.datetime.combine(datetime.date.today() + timedelta(days=1), datetime.time(8, 00))
#             f = cloudlight.fadecandy.ModeFormV2(mode, settings, mute=g.redis.read('player:muted'),
#                                                 sleep_timer=0, formdata=None, schedule_data={'at': morning,
#                                                                                              'repeat': True,
#                                                                                              'at2':morning,
#                                                                                              'time':morning,
#                                                                                              'date':morning})
#         return render_template('index.html', title=_('Cloudlight'), modes=modes, form=f, active_mode=g.mode)
#     else:
#         mode = g.redis.read('lamp:mode')
#         morning = datetime.datetime.combine(datetime.date.today() + timedelta(days=1), datetime.time(8, 00))
#         f = cloudlight.fadecandy.ModeFormV2(mode, g.redis.read(f'lamp:{mode}:settings'),
#                                             schedule_data={'at': morning, 'repeat': True,
#                                                            'at2': morning,
#                                                            'time': morning,
#                                                            'date': morning
#                                                            })
#
#     return render_template('index.html', title=_('Cloudlight'), modes=modes, form=f, active_mode=g.mode)
#
#
# @bp.route('/rediscontrol', methods=['POST', 'GET'])
# def rediscontrol():
#     """Handle read and write requests for redis keys"""
#     if request.method == 'POST':
#         try:
#             val = float(request.form['value'])
#         except ValueError:
#             val = request.form['value']
#         try:
#             current_app.redis.store(request.form['source'].partition(':')[2], val)
#             return jsonify({'success': True})
#         except:
#             current_app.logger.error('post error', exc_info=True)
#     else:
#         try:
#             return jsonify({'value': current_app.redis.read(request.args.get('key'))})
#         except:
#             current_app.logger.error(f'get error {request.args}', exc_info=True)
#     return bad_request('control failed')
#
#
#
#
# # Controls need to be named with their redis key
# @bp.route('/redisdata', methods=['GET'])
# def redisdata():
#     from ....config import schema_keys
#     keys = schema_keys()
#
#     # @copy_current_request_context
#     def _stream():
#         import cloudlight.cloudredis as clr
#         r = clr.setup_redis(use_schema=False, module=False)
#         i=0
#         for k, v in r.listen(keys):
#             print(k,v)
#             yield f"event:update\nretry:5\ndata: {json.dumps({k:v})}\n\n"
#             i+=1
#             if i==5:
#                 break
#         time.sleep(1)
#
#     return current_app.response_class(_stream(), mimetype="text/event-stream")
#
# @bp.route('/redispoll', methods=['GET'])
# def redispoll():
#     from ....config import schema_keys
#     return jsonify(g.redis.read(schema_keys()))
#
#
# @bp.route('/status')
# def status():
#     from ....config import schema_keys
#     system = get_system_status()
#
#     table = [('Setting', 'Value')]
#     table += [(k, k, v) for k, v in current_app.redis.read(schema_keys()).items()]
#
#     start = datetime.datetime.now() - timedelta(days=1)
#     times, vals = list(zip(*g.redis.range('temp:value_avg120000', start=start)))
#     timescpu, valscpu = list(zip(*g.redis.range('temp:cpu:value_avg120000', start=start)))
#     times = np.array(times, dtype='datetime64[ms]')
#     timescpu = np.array(timescpu, dtype='datetime64[ms]')
#     fig = go.Figure()
#     fig.add_trace(go.Scatter(x=times, y=vals, mode='lines', name='Internal'))
#     fig.add_trace(go.Scatter(x=timescpu, y=valscpu, mode='lines', name='CPU'))
#     fig.update_layout(title='Cloud Temps', xaxis_title='Time', yaxis_title='\N{DEGREE SIGN}F')
#     fig = json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)
#
#     return render_template('status.html', title=_('Status'), table=table, tempfig=fig, system_stat=system)
#
# #TODO add critical temp? todo make sliders responsive
# @bp.route('/settings', methods=['GET', 'POST'])
# def settings():
#     from .forms import CloudControl
#     r2f = {'speaker:keepalive': 'keepalive', 'lamp:overheated_limit': 'thermal_brightness',
#            'temp:alarm_threshold': 'thermal_limit', 'lamp:max_led_level': 'max_led_level'}
#     formdata = {r2f[k]: v for k, v in g.redis.read(r2f.keys()).items()}
#     f = CloudControl(data=formdata)
#     if request.method == 'POST' and f.validate_on_submit():
#         g.redis.store({k: f.data[v] for k, v in r2f.items()})
#     return render_template('settings.html', title=_('Settings'), form=f)
#
#
# @bp.route('/help')
# def help():
#     return render_template('help.html', title=_('Help'))
#
# @bp.route('/pihole')
# def pihole():
#     return redirect('https://cloudlight.local/admin')
#
#
# @bp.route('/modeform', methods=['POST'])
# def modeform():
#     mode = request.form['data']
#     try:
#         import datetime
#         morning = datetime.datetime.combine(datetime.date.today() + timedelta(days=1), datetime.time(8, 00))
#         form = cloudlight.fadecandy.ModeFormV2(mode, g.redis.read(f'lamp:{mode}:settings'),
#                                                schedule_data={'at': morning, 'repeat': True}, formdata=None)
#         return jsonify({'html': render_template('_mode_form.html', form=form, active_mode=g.mode)})
#     except KeyError:
#         return bad_request(f'"{mode}" is not known')
