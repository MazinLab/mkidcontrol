import os
import subprocess
from datetime import datetime
import plotly.graph_objects as go

import mkidcontrol.mkidredis
from mkidcontrol.util import setup_logging
from mkidcontrol.util import get_service as mkidcontrol_service

import numpy as np
import flask
from flask import render_template, flash, redirect, url_for, request, g, \
    jsonify, current_app, Response, copy_current_request_context
from flask_login import current_user, login_required
from flask_babel import _, get_locale
from flask_wtf import FlaskForm

from .. import db
# from .forms import *
from ..models import User, Post, Message, Notification
from . import bp
from .helpers import *
from ..api.errors import bad_request
import time, json, threading
import plotly
from datetime import timedelta
import datetime
import json
from rq.job import Job, NoSuchJobError
import pytz
import select

from mkidcontrol.controlflask.config import Config
import mkidcontrol.mkidredis as redis
from mkidcontrol.commands import COMMAND_DICT, SimCommand

from mkidcontrol.sim960Agent import SIM960_KEYS
from mkidcontrol.sim921Agent import SIM921_KEYS
from mkidcontrol.lakeshore240Agent import LAKESHORE240_KEYS
from mkidcontrol.hemttempAgent import HEMTTEMP_KEYS
from mkidcontrol.currentduinoAgent import CURRENTDUINO_KEYS
from .forms import *


TS_KEYS = ['status:temps:mkidarray:temp', 'status:temps:mkidarray:resistance', 'status:temps:lhetank',
           'status:temps:ln2tank', 'status:feedline1:hemt:gate-voltage-bias',
           'status:feedline2:hemt:gate-voltage-bias', 'status:feedline3:hemt:gate-voltage-bias',
           'status:feedline4:hemt:gate-voltage-bias', 'status:feedline5:hemt:gate-voltage-bias',
           'status:feedline1:hemt:drain-voltage-bias', 'status:feedline2:hemt:drain-voltage-bias',
           'status:feedline3:hemt:drain-voltage-bias', 'status:feedline4:hemt:drain-voltage-bias',
           'status:feedline5:hemt:drain-voltage-bias', 'status:feedline1:hemt:drain-current-bias',
           'status:feedline2:hemt:drain-current-bias', 'status:feedline3:hemt:drain-current-bias',
           'status:feedline4:hemt:drain-current-bias', 'status:feedline5:hemt:drain-current-bias',
           'status:device:sim960:hcfet-control-voltage', 'status:highcurrentboard:current',
           'status:device:sim960:current-setpoint', 'status:device:sim921:sim960-vout', 'status:device:sim960:vin',
           'status:temps:50k-stage:temp', 'status:temps:50k-stage:voltage', 'status:temps:3k-stage:temp',
           'status:temps:3k-stage:voltage', 'status:temps:1k-stage:temp', 'status:temps:1k-stage:resistance',
           'status:temps:device-stage:temp', 'status:temps:device-stage:resistance', 'status:magnet:current']

CHART_KEYS = {'Device T':'status:temps:device-stage:temp',
              '1K Stage':'status:temps:1k-stage:temp',
              '3K Stage':'status:temps:3k-stage:temp',
              '50K Stage':'status:temps:50k-stage:temp'}#,
              # 'Magnet I':'status:magnet:current'}

RAMP_SLOPE_KEY = 'device-settings:sim960:ramp-rate'
DERAMP_SLOPE_KEY = 'device-settings:sim960:deramp-rate'
SOAK_TIME_KEY = 'device-settings:sim960:soak-time'
SOAK_CURRENT_KEY = 'device-settings:sim960:soak-current'

KEYS = list(COMMAND_DICT.keys()) + \
       ['status:temps:50k-stage:temp',
        'status:temps:50k-stage:voltage',
        'status:temps:3k-stage:temp',
        'status:temps:3k-stage:voltage',
        'status:temps:1k-stage:temp',
        'status:temps:1k-stage:resistance',
        'status:temps:device-stage:temp',
        'status:temps:device-stage:resistance',
        'status:magnet:current']


DASHDATA = np.load('/mkidcontrol/mkidcontrol/frontend/dashboard_placeholder.npy')


redis.setup_redis(ts_keys=TS_KEYS)

log = setup_logging('controlDirector')


def guess_language(x):
    return 'en'

# TODO: Figure out best how to set the global redis instance from current_app. Currently (20 July 2022) it is not working
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
    TODO: Device Viewer - Currently has placeholder buttons/information/'view'
    """
    form = FlaskForm()
    if request.method == 'POST':
        return handle_validation(request, submission=True)

    d,l,c = initialize_sensors_plot(CHART_KEYS.keys())
    dd, dl, dc = view_array_data()
    cycleform = CycleControlForm()
    magnetform = MagnetControlForm()

    subkeys = [key for key in FIELD_KEYS.keys() if FIELD_KEYS[key]['type'] in ('magnet', 'cycle')]
    rtvkeys = [key for key in subkeys if FIELD_KEYS[key]['field_type'] in ('string')]
    updatingkeys = [[key, FIELD_KEYS[key]['key']] for key in FIELD_KEYS.keys() if FIELD_KEYS[key]['type'] in ('magnet')]

    return render_template('index.html', form=form, mag=magnetform, cyc=cycleform,
                           d=d, l=l, c=c, dd=dd, dl=dl, dc=dc, subkeys=subkeys, rtvkeys=rtvkeys,
                           updatingkeys=updatingkeys, sensorkeys=list(CHART_KEYS.values()))


@bp.route('/other_plots', methods=['GET'])
def other_plots():
    """
    Flask endpoint for 'other plots'. This page has ALL sensor plots in one place for convenience (in contrast to index,
    which only has one at a time).
    """
    print('going to other plots page!')
    form = FlaskForm()
    devt_d, devt_l, devt_c = initialize_sensor_plot('Device T')
    onek_d, onek_l, onek_c = initialize_sensor_plot('1K Stage')
    threek_d, threek_l, threek_c = initialize_sensor_plot('3K Stage')
    fiftyk_d, fiftyk_l, fiftyk_c = initialize_sensor_plot('50K Stage')
    # magc_d, magc_l, magc_c = initialize_sensor_plot('Magnet I')

    return render_template('other_plots.html', title='Other Plots', form=form,
                           devt_d=devt_d, devt_l=devt_l, devt_c=devt_c,
                           onek_d=onek_d, onek_l=onek_l, onek_c=onek_c,
                           threek_d=threek_d, threek_l=threek_l, threek_c=threek_c,
                           fiftyk_d=fiftyk_d, fiftyk_l=fiftyk_l, fiftyk_c=fiftyk_c)


@bp.route('/settings', methods=['GET', 'POST'])
def settings():
    """
    Flask endpoint for settings page. Handles setting changes for housekeeping instruments
    TODO: Readout settings (when we have a readout)
    """
    if request.method == 'POST':
        return handle_validation(request, submission=True)


    return render_template('settings.html', title='Settings')


@bp.route('/log_viewer', methods=['GET', 'POST'])
def log_viewer():
    """
    Flask endpoint for log viewer. This page is solely for observing the journalctl output from each agent.
    """
    form = FlaskForm()
    return render_template('log_viewer.html', title='Log Viewer', form=form)


@bp.route('/thermometry/<device>/<channel>', methods=['GET', 'POST'])
def thermometry(device, channel):
    title = redis.read(f'device-settings:{device}:input-channel-{channel.lower()}:name')
    from ....commands import LakeShoreCommand

    if request.method == 'POST':
        print(f"Form: {request.form}")
        for key in request.form.keys():
            print(f"{key} : {request.form.get(key)}")
            try:
                x = LakeShoreCommand(f"device-settings:{device}:input-channel-{request.form.get('channel').lower()}:{key.replace('_','-')}", request.form.get(key))
                print(x)
            except ValueError as e:
                print(e)

    if device == 'ls336':
        from ....lakeshore336Agent import RTDForm, DiodeForm, DisabledInputForm
        from ....commands import LS336InputSensor, ALLOWED_336_CHANNELS, LakeShoreCommand

        sensor = LS336InputSensor(channel=channel, redis=redis)
        if sensor.sensor_type == "NTC RTD":
            form = RTDForm(channel=f"{channel}", name=sensor.name, sensor_type=sensor.sensor_type, units=sensor.units,
                           curve=sensor.curve, autorange=bool(sensor.autorange_enabled),
                           compensation=bool(sensor.compensation), input_range=sensor.input_range)
        elif sensor.sensor_type == "Diode":
            form = DiodeForm(channel=f"{channel}", name=sensor.name, sensor_type={sensor.sensor_type}, units=sensor.units,
                              curve=sensor.curve, autorange=bool(sensor.autorange_enabled),
                              compensation=bool(sensor.compensation), input_range=sensor.input_range)
        elif sensor.sensor_type == "Disabled":
            form = DisabledInputForm(channel=f"{channel}", name=sensor.name)
    elif device == 'ls372':
        return redirect(url_for('main.page_not_found'))
    return render_template('thermometry.html', title=f"{title} Thermometer", form=form)


@bp.route('/services')
def services():
    from mkidcontrol.util import get_services as mkidcontrol_services
    services = mkidcontrol_services()
    # TODO: Figure this block out with g.redis.
    #  Until then, exporting = False
    # try:
    #     job = Job.fetch('email-logs', connection=g.redis)
    #     exporting = job.get_status() in ('queued', 'started', 'deferred', 'scheduled')
    # except NoSuchJobError:
    #     exporting = False
    exporting = False
    return render_template('services.html', title=_('Services'), services=services.values(), exporting=exporting)


@bp.route('/service', methods=['POST', 'GET'])
# @login_required
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
# @login_required
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
# @login_required
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
    from ....lakeshore336Agent import RTDForm, DiodeForm, DisabledInputForm
    from mkidcontrol.commands import LS336InputSensor, ENABLED_336_CHANNELS, ALLOWED_336_CHANNELS, LakeShoreCommand
    """
    Test area for trying out things before implementing them on a page
    """
    if request.method == 'POST':
        print(f"Form: {request.form}")
        for key in request.form.keys():
            print(f"{key} : {request.form.get(key)}")
            try:
                x = LakeShoreCommand(f"device-settings:ls336:input-channel-{request.form.get('channel').lower()}:{key.replace('_','-')}", request.form.get(key))
                print(x)
            except ValueError as e:
                print(e)
    # schedule = Schedule()
    forms = []
    for ch in ALLOWED_336_CHANNELS:
        sensor = LS336InputSensor(channel=ch, redis=redis)
        if sensor.sensor_type == "NTC RTD":
            forms.append([ch, RTDForm(channel=f"{ch}", name="NTC RTD", sensor_type=sensor.sensor_type, units=sensor.units, curve=sensor.curve,
                                 autorange=bool(sensor.autorange_enabled), compensation=bool(sensor.compensation),
                                 input_range=sensor.input_range)])
        elif sensor.sensor_type == "Diode":
            forms.append([ch, DiodeForm(channel=f"{ch}", name="Diode", sensor_type=sensor.sensor_type, units=sensor.units, curve=sensor.curve,
                                   autorange=bool(sensor.autorange_enabled), compensation=bool(sensor.compensation),
                                   input_range=sensor.input_range)])
        elif sensor.sensor_type == "Disabled":
            forms.append([ch, DisabledInputForm(channel=f"{ch}", name="Disabled Input")])
    return render_template('test_page.html', title='Test Page', forms=forms) #, schedule=schedule)


@bp.route('/404', methods=['GET', 'POST'])
def page_not_found():
    return render_template('/errors/404.html'), 404

# ----------------------------------- Helper Functions Below -----------------------------------
@bp.route('/dashlistener', methods=["GET"])
def dashlistener():
    """
    listener is a function that implements the python (server) side of a server sent event (SSE) communication protocol
    where data can be streamed directly to the flask app.
    """
    def stream():
        while True:
            time.sleep(.5)
            d, _, _ = view_array_data()
            t = time.time()
            mes = json.dumps({'data':d, 'time':datetime.datetime.fromtimestamp(t).strftime("%H:%M:%S.%f")[:-4]})
            msg = f"retry:5\ndata: {mes}\n\n"
            yield msg
    return Response(stream(), mimetype='text/event-stream', content_type='text/event-stream')


@bp.route('/listener', methods=["GET"])
def listener():
    """
    listener is a function that implements the python (server) side of a server sent event (SSE) communication protocol
    where data can be streamed directly to the flask app.
    """
    def stream():
        while True:
            time.sleep(.75)
            x = redis.read(KEYS)
            x = json.dumps(x)
            msg = f"retry:5\ndata: {x}\n\n"
            yield msg
    return Response(stream(), mimetype='text/event-stream', content_type='text/event-stream')


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


@bp.route('/validatecmd', methods=['POST'])
def validate_cmd_change():
    """
    Flask endpoint which is called from an AJAX request when new data is typed/entered into a submittable field. This
    will then report back if the value is allowed or not and report that to the user accordingly (with a check or X)
    """
    return handle_validation(request)


def initialize_sensor_plot(title):
    """
    :param key: Redis key plot data is needed for
    :param title: Plot title. If '-', not used
    :param typ: <'new'|'old'> Type of updating required. 'new' gives the most recent point. 'old' gives up to 30 minutes of data.
    :return: data to be plotted.
    """
    last_tval = time.time() # In seconds
    first_tval = int((last_tval - 1800) * 1000)  # Allow data from up to 30 minutes beforehand to be plotted (30 m = 1800 s)
    ts = np.array(redis.mkr_range(CHART_KEYS[title], f"{first_tval}", '+'))
    times = [datetime.datetime.fromtimestamp(t/1000).strftime("%H:%M:%S") for t in ts[:,0]]
    vals = list(ts[:,1])
    if len(times) == 0:
        val = redis.read(CHART_KEYS[title])
        times = [datetime.datetime.fromtimestamp(val[0] / 1000).strftime("%H:%M:%S")]
        vals = [val[1]]

    plot_data = [{'x': times,'y': vals,'name': title}]
    plot_layout = {'title': title}
    plot_config = {'responsive': True}
    d = json.dumps(plot_data, cls=plotly.utils.PlotlyJSONEncoder)
    l = json.dumps(plot_layout, cls=plotly.utils.PlotlyJSONEncoder)
    c = json.dumps(plot_config, cls=plotly.utils.PlotlyJSONEncoder)
    return d, l, c


def initialize_sensors_plot(titles):
    last_tval = time.time()
    first_tval = int((last_tval - 1800) * 1000)
    keys = [CHART_KEYS[i] for i in titles]
    timestreams = [np.array(redis.mkr_range(key, f"{first_tval}", "+")) for key in keys]
    times = [[datetime.datetime.fromtimestamp(t / 1000).strftime("%H:%M:%S") for t in ts[:, 0]] for ts in timestreams]
    vals = [list(ts[:, 1]) for ts in timestreams]

    update_menus = []
    for n, t in enumerate(titles):
        visible = [False] * len(titles)
        visible[n] = True
        t_dict = dict(label=str(t),
                      method='update',
                      args=[{'visible': visible}])#, {'title': t}])
        update_menus.append(t_dict)

    plot_data = [{'x': i, 'y': j, 'name': t, 'mode': 'lines', 'visible': False} for i, j, t in
                 zip(times, vals, titles)]
    plot_data[0]['visible'] = True
    plot_layout = dict(updatemenus=list([dict(buttons=update_menus, x=0.01, xanchor='left', y=1.1, yanchor='top')]))
    plot_config = {'responsive': True}
    d = json.dumps(plot_data, cls=plotly.utils.PlotlyJSONEncoder)
    l = json.dumps(plot_layout, cls=plotly.utils.PlotlyJSONEncoder)
    c = json.dumps(plot_config, cls=plotly.utils.PlotlyJSONEncoder)
    return d, l, c


def view_array_data():
    """
    Placeholding function to grab a frame from a (hard-coded, previously made) temporal drizzle to display as the
    'device view' on the homepage of the flask application.
    """
    frame_to_use = 100
    x = DASHDATA[frame_to_use][100:170, 100:170]
    noise = 25 * np.random.randn(70, 70)
    y = x + noise
    z = [{'z': y.tolist(), 'type': 'heatmap', 'showscale':False}]
    plot_layout = {'title': 'Array'}
    plot_config = {'responsive': True}
    d = json.dumps(z, cls=plotly.utils.PlotlyJSONEncoder)
    l = json.dumps(plot_layout, cls=plotly.utils.PlotlyJSONEncoder)
    c = json.dumps(plot_config, cls=plotly.utils.PlotlyJSONEncoder)
    return d, l, c


def parse_schedule_cooldown(schedule_time):
    """
    Takes a string input from the schedule cooldown field and parses it to determine if it is in a proper format to be
    used as a time for scheduling a cooldown.
    Returns a timestamp in seconds (to send to the SIM960 agent for scheduling), a datetime object (for reporting to
    flask page), and time until the desired cold time in seconds (to check for it being allowable)
    """
    t = schedule_time.split(" ")
    now = datetime.datetime.now()
    year = now.year
    month = now.month
    day = now.day
    if len(t) == 2:
        sked_type = 'date'
    else:
        sked_type = 'time'
    if sked_type == 'date':
        d = t[0].split('/')
        print(d, len(d))
        month = int(d[0])
        day = int(d[1])
        print(month, day)
        if len(d) == 2:
            year = now.year
        elif (len(d[2]) == 2) and (d[2][0:2] != 20):
            year = int('20'+d[2])
        else:
            year = int(d[2])
        tval = t[1].split(":")
        hr = int(tval[0])
        minute = int(tval[1])
        print(f"year: {year}, month: {month}, day: {day}")
    else:
        tval = t[0].split(":")
        hr = int(tval[0])
        minute = int(tval[1])

    be_cold_at = datetime.datetime(year, month, day, hr, minute)
    tdelta = (be_cold_at - datetime.datetime.now()).total_seconds()
    ts = be_cold_at.timestamp()
    return ts, be_cold_at, tdelta


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
# @login_required
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
# @login_required
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
# # Controls need to be named with their redis key
# @bp.route('/plotdata', methods=['GET'])
# @login_required
# def plotdata():
#
#     # @copy_current_request_context
#     def _stream():
#         since = None
#         import cloudlight.cloudredis as clr
#         r = clr.setup_redis(use_schema=False, module=False)
#         while True:
#             kind = 'full' if since is None else 'partial'
#             start = datetime.datetime.now() - timedelta(days=.5) if not since else since
#             times, vals = list(zip(*r.range('temp:value_avg120000', start=start)))
#             timescpu, valscpu = list(zip(*r.range('temp:cpu:value_avg120000', start=start)))
#             # since = times[-1]
#             times = np.array(times, dtype='datetime64[ms]')
#             timescpu = np.array(timescpu, dtype='datetime64[ms]')
#             if kind == 'full':
#                 fig = go.Figure()
#                 fig.add_trace(go.Scatter(x=times, y=vals, mode='lines', name='Internal'))
#                 fig.add_trace(go.Scatter(x=timescpu, y=valscpu, mode='lines', name='CPU'))
#                 fig.update_layout(title='Cloud Temps', xaxis_title='Time', yaxis_title='\N{DEGREE SIGN}F')
#                 figdata = json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)
#             else:
#                 figdata = {'x': times, 'y': vals}
#
#             data = {'id': f'temp-plot', 'kind': kind, 'data': figdata}
#             yield f"event:plot\nretry:5\ndata: {json.dumps(data)}\n\n"
#             time.sleep(15)
#
#     return current_app.response_class(_stream(), mimetype="text/event-stream")
#
#
# # Controls need to be named with their redis key
# @bp.route('/redisdata', methods=['GET'])
# @login_required
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
# @login_required
# def redispoll():
#     from ....config import schema_keys
#     return jsonify(g.redis.read(schema_keys()))
#
#
# @bp.route('/system', methods=['POST'])
# @login_required
# def system():
#     """data: shutdown|reboot|reinit """
#     cmd = request.form.get('data', '')
#     if cmd in ('shutdown', 'reboot'):
#         subprocess.Popen(['/home/pi/.local/bin/cloud-service-control', cmd])
#         flash(f'System going offline for {cmd}')
#         return jsonify({'success': True})
#     elif cmd == 'reinit':
#         import cloudlight.cloudredis as clr
#         clr.setup_redis(module=False, clear=True, use_schema=True)
#         return jsonify({'success': True})
#     else:
#         return bad_request('Invalid shutdown command')
#
#
# @bp.route('/task', methods=['GET', 'POST'])
# @login_required
# def task():
#     if request.method == 'POST':
#         id = request.form.get('id')
#         if id != 'email-logs':
#             return bad_request('Unknown task')
#         try:
#             job = Job.fetch(id, connection=g.redis.redis)
#             if job.is_failed or job.is_finished:
#                 job.delete()
#                 job = None
#         except NoSuchJobError:
#             job = None
#         if job:
#             flash(_(f'Task "{id} is currently pending'))
#             return bad_request(f'Task "{id}" in progress')
#         else:
#             current_app.task_queue.enqueue(f"cloudlight.cloudflask.app.tasks.{id.replace('-', '_')}", job_id=id)
#             return jsonify({'success': True})
#
#     else:
#         id = request.args.get('id', '')
#         if not id:
#             return bad_request('Task id required')
#         try:
#             job = Job.fetch(id, connection=g.redis.redis)
#         except NoSuchJobError:
#             return bad_request('Unknown task')
#         status = job.get_status()
#         return jsonify({'done': status == 'finished', 'error': status != 'finished',
#                         'progress': job.meta.get('progress', 0)})
#
#
# @bp.route('/service', methods=['POST', 'GET'])
# @login_required
# def service():
#     """start, stop, enable, disable, restart"""
#     name = request.args.get('name', '')
#     try:
#         service = cloudlight_service(name)
#     except ValueError:
#         return bad_request(f'Service "{name}" does not exist.')
#     if request.method == 'POST':
#         service.control(request.form['data'])
#         # flash('Executing... updating in 5')
#         return jsonify({'success': True})
#     else:
#         return jsonify(service.status_dict())
#
#
# @bp.route('/status')
# @login_required
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
#
# #TODO add critical temp? todo make sliders responsive
# @bp.route('/settings', methods=['GET', 'POST'])
# @login_required
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
# @bp.route('/off', methods=['POST'])
# @login_required
# def off():
#     g.redis.store('lamp:mode', 'off')
#     canceled = 'sleep_timer' in current_app.scheduler
#     current_app.scheduler.cancel('sleep_timer')
#     if canceled:
#         flash('Sleep timer canceled.')
#     return jsonify({'success': True})
#
#
# @bp.route('/help')
# def help():
#     return render_template('help.html', title=_('Help'))
#
#
# @bp.route('/services')
# @login_required
# def services():
#     from cloudlight.util import get_services as cloudlight_services
#     services = cloudlight_services()
#     try:
#         job = Job.fetch('email-logs', connection=g.redis.redis)
#         exporting = job.get_status() in ('queued', 'started', 'deferred', 'scheduled')
#     except NoSuchJobError:
#         exporting = False
#     return render_template('services.html', title=_('Services'), services=services.values(), exporting=exporting)
#
#
# @bp.route('/pihole')
# def pihole():
#     return redirect('https://cloudlight.local/admin')
#
#
# @bp.route('/modeform', methods=['POST'])
# @login_required
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
