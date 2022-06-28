import os
import subprocess
from datetime import datetime
import plotly.graph_objects as go

import numpy as np
from flask import render_template, flash, redirect, url_for, request, g, \
    jsonify, current_app, Response, copy_current_request_context
from flask_login import current_user, login_required
from flask_babel import _, get_locale

import cloudlight.fadecandy
from cloudlight.util import get_services as cloudlight_services
from cloudlight.util import get_service as cloudlight_service
from app import db
from .forms import EmptyForm
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
from cloudlight.fadecandy import EFFECTS
from cloudlight.util import get_system_status



def guess_language(x):
    return 'en'


@bp.before_app_request
def before_request():
    if current_user.is_authenticated:
        current_user.last_seen = datetime.datetime.utcnow()
        db.session.commit()
    g.locale = str(get_locale())
    redis = current_app.redis
    # redis = cloudlight.cloudredis.setup_redis(use_schema=False, module=False)
    g.redis = redis
    g.mode = redis.read('lamp:mode')


@bp.after_request
def add_header(response):
    response.headers['Cache-Control'] = 'no-cache, no-store'
    return response


@bp.route('/', methods=['GET', 'POST'])
@bp.route('/index', methods=['GET', 'POST'])
@login_required
def index():
    modes = tuple((key, e.name) for key, e in cloudlight.fadecandy.EFFECTS.items())
    if request.method == 'POST':
        mode = request.form['mode_key']
        f = cloudlight.fadecandy.ModeFormV2(mode)
        if mode == 'off':
            g.redis.store(f'lamp:mode', mode)
            g.redis.store(f'lamp:off:settings', EFFECTS['off'].defaults)
        elif not f.validate_on_submit():
            current_app.logger.warning(f'Validation of {f} failed.')
            print(f)
            print(f.errors)
            print(f.data)
            print('request form')
            print(request.form)
        else:

            settings = f.settings.data
            if f.schedule_data.schedule.data or f.schedule_data.clear.data:
                current_app.logger.debug('Clearing Scheduled lamp event')
                canceled = 'schedule' in current_app.scheduler
                current_app.scheduler.cancel('schedule')
                if not f.schedule_data.clear.data:
                    current_app.logger.debug(f'Scheduling {mode} at {f.schedule_data.at.data}. '
                                             f'repeat={f.schedule_data.repeat.data}')
                    current_app.scheduler.schedule(f.schedule_data.at.data.astimezone(pytz.utc),
                                                   repeat=0 if not f.schedule_data.repeat.data else None,
                                                   interval=24 * 3600, id='schedule',
                                                   func=f"cloudlight.cloudflask.app.tasks.lamp_to_mode", args=(mode,),
                                                   kwargs={'mode_settings': settings,
                                                           'mute': f.mute.data if 'mute' in f else None})
                    date = f.schedule_data.at.data.strftime('%I:%M %p on %m/%d/%Y' if not f.schedule_data.repeat.data
                                                            else 'every day at %I:%M %p')
                    flashmsg = f'Effect {EFFECTS[mode].name} scheduled for {date}'
                    flash(flashmsg + (' (replaced previous event).' if canceled else '.'))
                elif canceled:
                    flash('Scheduled effect canceled.')
            else:
                if f.reset.data:  # reset the effect to the defaults
                    g.redis.store(f'lamp:{mode}:settings', EFFECTS[mode].defaults)
                else:  # f.save or f.enable either way update
                    g.redis.store(f'lamp:{mode}:settings', settings)

                if g.redis.read('lamp:mode') == mode:
                    g.redis.store(f'lamp:settings', True, publish_only=True)

                if 'mute' in f:
                    g.redis.store('player:muted', f.mute.data)

                if f.submit.data:
                    canceled = 'sleep_timer' in current_app.scheduler
                    current_app.scheduler.cancel('sleep_timer')
                    if f.sleep_timer.data:
                        current_app.logger.debug(f'Scheduling sleep timer to turn off in {f.sleep_timer.data} minutes.')
                        current_app.scheduler.enqueue_in(timedelta(minutes=f.sleep_timer.data),
                                                         f"cloudlight.cloudflask.app.tasks.lamp_to_mode",
                                                         'off', job_id='sleep_timer')
                    if f.sleep_timer.data:
                        if canceled:
                            flash('Will now turn off in {f.sleep_timer.data:.0f} minutes.')
                        else:
                            flash(f'{EFFECTS[mode].name} will fade out in {f.sleep_timer.data:.0f} minutes.')
                    elif canceled:
                        flash('Sleep timer canceled.')

                    g.redis.store(f'lamp:mode', mode)
            settings = g.redis.read(f'lamp:{mode}:settings')

            morning = datetime.datetime.combine(datetime.date.today() + timedelta(days=1), datetime.time(8, 00))
            f = cloudlight.fadecandy.ModeFormV2(mode, settings, mute=g.redis.read('player:muted'),
                                                sleep_timer=0, formdata=None, schedule_data={'at': morning,
                                                                                             'repeat': True,
                                                                                             'at2':morning,
                                                                                             'time':morning,
                                                                                             'date':morning})
        return render_template('index.html', title=_('Cloudlight'), modes=modes, form=f, active_mode=g.mode)
    else:
        mode = g.redis.read('lamp:mode')
        morning = datetime.datetime.combine(datetime.date.today() + timedelta(days=1), datetime.time(8, 00))
        f = cloudlight.fadecandy.ModeFormV2(mode, g.redis.read(f'lamp:{mode}:settings'),
                                            schedule_data={'at': morning, 'repeat': True,
                                                           'at2': morning,
                                                           'time': morning,
                                                           'date': morning
                                                           })

    return render_template('index.html', title=_('Cloudlight'), modes=modes, form=f, active_mode=g.mode)


@bp.route('/rediscontrol', methods=['POST', 'GET'])
@login_required
def rediscontrol():
    """Handle read and write requests for redis keys"""
    if request.method == 'POST':
        try:
            val = float(request.form['value'])
        except ValueError:
            val = request.form['value']
        try:
            current_app.redis.store(request.form['source'].partition(':')[2], val)
            return jsonify({'success': True})
        except:
            current_app.logger.error('post error', exc_info=True)
    else:
        try:
            return jsonify({'value': current_app.redis.read(request.args.get('key'))})
        except:
            current_app.logger.error(f'get error {request.args}', exc_info=True)
    return bad_request('control failed')


# Controls need to be named with their redis key
@bp.route('/plotdata', methods=['GET'])
@login_required
def plotdata():

    # @copy_current_request_context
    def _stream():
        since = None
        import cloudlight.cloudredis as clr
        r = clr.setup_redis(use_schema=False, module=False)
        while True:
            kind = 'full' if since is None else 'partial'
            start = datetime.datetime.now() - timedelta(days=.5) if not since else since
            times, vals = list(zip(*r.range('temp:value_avg120000', start=start)))
            timescpu, valscpu = list(zip(*r.range('temp:cpu:value_avg120000', start=start)))
            # since = times[-1]
            times = np.array(times, dtype='datetime64[ms]')
            timescpu = np.array(timescpu, dtype='datetime64[ms]')
            if kind == 'full':
                fig = go.Figure()
                fig.add_trace(go.Scatter(x=times, y=vals, mode='lines', name='Internal'))
                fig.add_trace(go.Scatter(x=timescpu, y=valscpu, mode='lines', name='CPU'))
                fig.update_layout(title='Cloud Temps', xaxis_title='Time', yaxis_title='\N{DEGREE SIGN}F')
                figdata = json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)
            else:
                figdata = {'x': times, 'y': vals}

            data = {'id': f'temp-plot', 'kind': kind, 'data': figdata}
            yield f"event:plot\nretry:5\ndata: {json.dumps(data)}\n\n"
            time.sleep(15)

    return current_app.response_class(_stream(), mimetype="text/event-stream")


# Controls need to be named with their redis key
@bp.route('/redisdata', methods=['GET'])
@login_required
def redisdata():
    from ....config import schema_keys
    keys = schema_keys()

    # @copy_current_request_context
    def _stream():
        import cloudlight.cloudredis as clr
        r = clr.setup_redis(use_schema=False, module=False)
        i=0
        for k, v in r.listen(keys):
            print(k,v)
            yield f"event:update\nretry:5\ndata: {json.dumps({k:v})}\n\n"
            i+=1
            if i==5:
                break
        time.sleep(1)

    return current_app.response_class(_stream(), mimetype="text/event-stream")

@bp.route('/redispoll', methods=['GET'])
@login_required
def redispoll():
    from ....config import schema_keys
    return jsonify(g.redis.read(schema_keys()))


@bp.route('/system', methods=['POST'])
@login_required
def system():
    """data: shutdown|reboot|reinit """
    cmd = request.form.get('data', '')
    if cmd in ('shutdown', 'reboot'):
        subprocess.Popen(['/home/pi/.local/bin/cloud-service-control', cmd])
        flash(f'System going offline for {cmd}')
        return jsonify({'success': True})
    elif cmd == 'reinit':
        import cloudlight.cloudredis as clr
        clr.setup_redis(module=False, clear=True, use_schema=True)
        return jsonify({'success': True})
    else:
        return bad_request('Invalid shutdown command')


@bp.route('/task', methods=['GET', 'POST'])
@login_required
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
            current_app.task_queue.enqueue(f"cloudlight.cloudflask.app.tasks.{id.replace('-', '_')}", job_id=id)
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


@bp.route('/service', methods=['POST', 'GET'])
@login_required
def service():
    """start, stop, enable, disable, restart"""
    name = request.args.get('name', '')
    try:
        service = cloudlight_service(name)
    except ValueError:
        return bad_request(f'Service "{name}" does not exist.')
    if request.method == 'POST':
        service.control(request.form['data'])
        # flash('Executing... updating in 5')
        return jsonify({'success': True})
    else:
        return jsonify(service.status_dict())


@bp.route('/status')
@login_required
def status():
    from ....config import schema_keys
    system = get_system_status()

    table = [('Setting', 'Value')]
    table += [(k, k, v) for k, v in current_app.redis.read(schema_keys()).items()]

    start = datetime.datetime.now() - timedelta(days=1)
    times, vals = list(zip(*g.redis.range('temp:value_avg120000', start=start)))
    timescpu, valscpu = list(zip(*g.redis.range('temp:cpu:value_avg120000', start=start)))
    times = np.array(times, dtype='datetime64[ms]')
    timescpu = np.array(timescpu, dtype='datetime64[ms]')
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=times, y=vals, mode='lines', name='Internal'))
    fig.add_trace(go.Scatter(x=timescpu, y=valscpu, mode='lines', name='CPU'))
    fig.update_layout(title='Cloud Temps', xaxis_title='Time', yaxis_title='\N{DEGREE SIGN}F')
    fig = json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)

    return render_template('status.html', title=_('Status'), table=table, tempfig=fig, system_stat=system)


#TODO add critical temp? todo make sliders responsive
@bp.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    from .forms import CloudControl
    r2f = {'speaker:keepalive': 'keepalive', 'lamp:overheated_limit': 'thermal_brightness',
           'temp:alarm_threshold': 'thermal_limit', 'lamp:max_led_level': 'max_led_level'}
    formdata = {r2f[k]: v for k, v in g.redis.read(r2f.keys()).items()}
    f = CloudControl(data=formdata)
    if request.method == 'POST' and f.validate_on_submit():
        g.redis.store({k: f.data[v] for k, v in r2f.items()})
    return render_template('settings.html', title=_('Settings'), form=f)


@bp.route('/off', methods=['POST'])
@login_required
def off():
    g.redis.store('lamp:mode', 'off')
    canceled = 'sleep_timer' in current_app.scheduler
    current_app.scheduler.cancel('sleep_timer')
    if canceled:
        flash('Sleep timer canceled.')
    return jsonify({'success': True})


@bp.route('/help')
def help():
    return render_template('help.html', title=_('Help'))


@bp.route('/services')
@login_required
def services():
    from cloudlight.util import get_services as cloudlight_services
    services = cloudlight_services()
    try:
        job = Job.fetch('email-logs', connection=g.redis.redis)
        exporting = job.get_status() in ('queued', 'started', 'deferred', 'scheduled')
    except NoSuchJobError:
        exporting = False
    return render_template('services.html', title=_('Services'), services=services.values(), exporting=exporting)


@bp.route('/pihole')
def pihole():
    return redirect('https://cloudlight.local/admin')


@bp.route('/modeform', methods=['POST'])
@login_required
def modeform():
    mode = request.form['data']
    try:
        import datetime
        morning = datetime.datetime.combine(datetime.date.today() + timedelta(days=1), datetime.time(8, 00))
        form = cloudlight.fadecandy.ModeFormV2(mode, g.redis.read(f'lamp:{mode}:settings'),
                                               schedule_data={'at': morning, 'repeat': True}, formdata=None)
        return jsonify({'html': render_template('_mode_form.html', form=form, active_mode=g.mode)})
    except KeyError:
        return bad_request(f'"{mode}" is not known')


@bp.route('/notifications')
@login_required
def notifications():
    since = request.args.get('since', 0.0, type=float)
    notifications = current_user.notifications.filter(
        Notification.timestamp > since).order_by(Notification.timestamp.asc())
    return jsonify([{'name': n.name, 'data': n.get_data(), 'timestamp': n.timestamp} for n in notifications])
