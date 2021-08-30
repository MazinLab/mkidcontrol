"""
TODO: Make buttons on index page do stuff (actually publish redis commands)
TODO: Determine EXACTLY which fields need warning signs
TODO: Fix errors if redis range is empty

# TODO: Clean up plotting/streaming
# TODO: Condense/make more sensible validation handling
# TODO: Try to remove hardcoding as best as possible

# TODO: Lightcurve from pixel!
"""

import flask
from flask_wtf import FlaskForm
from flask_bootstrap import Bootstrap
from flask import request, redirect, url_for, render_template, jsonify, Response
import numpy as np
import time, datetime
import json
import plotly
from logging import getLogger
import sys
import subprocess
import select


import picturec.util as util
from picturec.frontend.config import Config
import picturec.pcredis as redis
from picturec.devices import COMMAND_DICT, SimCommand
import picturec.currentduinoAgent as heatswitch

from picturec.sim960Agent import SIM960_KEYS
from picturec.sim921Agent import SIM921_KEYS
from picturec.lakeshore240Agent import LAKESHORE240_KEYS
from picturec.hemttempAgent import HEMTTEMP_KEYS
from picturec.currentduinoAgent import CURRENTDUINO_KEYS
from picturec.frontend.customForms import CycleControlForm, MagnetControlForm, SIM921SettingForm, SIM960SettingForm, HeatswitchToggle, TestForm, \
    SIM921_SETTING_KEYS, SIM960_SETTING_KEYS, HEATSWITCH_SETTING_KEYS, MAGNET_COMMAND_FORM_KEYS, CYCLE_KEYS, FIELD_KEYS
# util.setup_logging('piccDirector')

app = flask.Flask(__name__)
app.logger.setLevel('DEBUG')
bootstrap = Bootstrap(app)
app.config.from_object(Config)

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
           'status:device:sim960:current-setpoint', 'status:device:sim921:sim960-vout', 'status:device:sim960:vin']

CHART_KEYS = {'Device T':'status:temps:mkidarray:temp',
              'LHe T':'status:temps:lhetank',
              'LN2 T':'status:temps:ln2tank',
              'Magnet I':'status:device:sim960:current-setpoint',
              'Measured I':'status:highcurrentboard:current'}

RAMP_SLOPE_KEY = 'device-settings:sim960:ramp-rate'
DERAMP_SLOPE_KEY = 'device-settings:sim960:deramp-rate'
SOAK_TIME_KEY = 'device-settings:sim960:soak-time'
SOAK_CURRENT_KEY = 'device-settings:sim960:soak-current'

KEYS = SIM921_KEYS + SIM960_KEYS + LAKESHORE240_KEYS + CURRENTDUINO_KEYS + HEMTTEMP_KEYS + list(COMMAND_DICT.keys())


DASHDATA = np.load('/picturec/picturec/frontend/dashboard_placeholder.npy')


redis.setup_redis(create_ts_keys=TS_KEYS)


@app.route('/', methods=['GET', 'POST'])
@app.route('/main', methods=['GET', 'POST'])
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


@app.route('/other_plots', methods=['GET'])
def other_plots():
    """
    Flask endpoint for 'other plots'. This page has ALL sensor plots in one place for convenience (in contrast to index,
    which only has one at a time).
    """
    form = FlaskForm()
    lhe_d, lhe_l, lhe_c = initialize_sensor_plot('LHe T')
    ln2_d, ln2_l, ln2_c = initialize_sensor_plot('LN2 T')
    devt_d, devt_l, devt_c = initialize_sensor_plot('Device T')
    magc_d, magc_l, magc_c = initialize_sensor_plot('Measured I')
    smagc_d, smagc_l, smagc_c = initialize_sensor_plot('Magnet I')
    return render_template('other_plots.html', title='Other Plots', form=form,
                           lhe_d=lhe_d, lhe_l=lhe_l, lhe_c=lhe_c, ln2_d=ln2_d, ln2_l=ln2_l, ln2_c=ln2_c,
                           devt_d=devt_d, devt_l=devt_l, devt_c=devt_c, magc_d=magc_d, magc_l=magc_l, magc_c=magc_c,
                           smagc_d=smagc_d, smagc_l=smagc_l, smagc_c=smagc_c)


@app.route('/settings', methods=['GET', 'POST'])
def settings():
    """
    Flask endpoint for settings page. Handles setting changes for housekeeping instruments
    TODO: Readout settings (when we have a readout)
    """
    if request.method == 'POST':
        return handle_validation(request, submission=True)

    sim921form = SIM921SettingForm()
    sim960form = SIM960SettingForm()
    forms = [sim921form, sim960form]
    hsbutton = HeatswitchToggle()

    subkeys = [key for key in FIELD_KEYS.keys() if FIELD_KEYS[key]['type'] in ('sim921', 'sim960', 'heatswitch')]
    rtvkeys = [key for key in subkeys if FIELD_KEYS[key]['field_type'] in ('string')]
    updatingkeys = [[key, FIELD_KEYS[key]['key']] for key in FIELD_KEYS.keys() if FIELD_KEYS[key]['type'] in ('sim921', 'sim960')]
    return render_template('settings.html', title='Settings', hs=hsbutton, forms=forms,
                           subkeys=subkeys, rtvkeys=rtvkeys, updatingkeys=updatingkeys)


@app.route('/log_viewer', methods=['GET', 'POST'])
def log_viewer():
    """
    Flask endpoint for log viewer. This page is solely for observing the journalctl output from each agent.
    """
    form = FlaskForm()
    return render_template('log_viewer.html', title='Log Viewer', form=form)


@app.route('/test_page', methods=['GET', 'POST'])
def test_page():
    """
    Test area for trying out things before implementing them on a page
    """
    tform = TestForm()
    if request.method == 'POST':
        print(request.form.get('x'), request.form.get('y'))
    return render_template('test_page.html', title='Test Page', form=tform)


# ----------------------------------- Helper Functions Below -----------------------------------
@app.route('/dashlistener', methods=["GET"])
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


@app.route('/listener', methods=["GET"])
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


@app.route('/journalctl_streamer/<service>')
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


@app.route('/validatecmd', methods=['POST'])
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
    ts = np.array(redis.pcr_range(CHART_KEYS[title], f"{first_tval}", '+'))
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
    timestreams = [np.array(redis.pcr_range(key, f"{first_tval}", "+")) for key in keys]
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
    x = DASHDATA[frame_to_use][110:175, 100:165]
    noise = 25 * np.random.randn(65, 65)
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

    app.logger.info(f"For field {id} (key: {key}), changing value to {value} with {field_type} methods.")
    if field_type in ('sim921', 'sim960', 'heatswitch', 'magnet'):
        try:
            s = SimCommand(key, value)
            is_legal = [True, '\u2713']
            if submission:
                if prefix_cmd:
                    app.logger.debug(f"Sending command:{key} -> {value}")
                    redis.publish(f"command:{key}", value, store=False)
                else:
                    app.logger.debug(f"Sending {key} -> {value}")
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
                    app.logger.debug(f"{key} -> {value}, {x[0]}")
                    redis.publish(key, x[0], store=False)
                if x[2] >= time_to_cool:
                    return jsonify({'cycle': True, 'key': 'command:be-cold-at', 'value': datetime.datetime.strftime(x[1], "%m/%d/%y %H:%M:%S"), 'legal': [True, '\u2713']})
                else:
                    return jsonify({'cycle': True, 'key': 'command:be-cold-at', 'value': datetime.datetime.strftime(x[1], "%m/%d/%y %H:%M:%S"), 'legal': [False, '\u2717']})
            except Exception as e:
                return jsonify({'cycle': True, 'key': 'command:be-cold-at', 'value': value, 'legal': [False, '\u2717']})
        else:
            if submission:
                app.logger.debug(f"{key} at {time.time()}")
                redis.publish(key, f"{time.time()}", store=False)
            return jsonify({'mag': True, 'key': key, 'value': time.strftime("%m/%d/%y %H:%M:%S"), 'legal': [True, '\u2713']})
    else:
        app.logger.critical(f"Field type '{field_type}' not implemented!")


if __name__ == "__main__":
    redis.setup_redis(create_ts_keys=TS_KEYS)
    app.run(port=8000, threaded=True, debug=True, ssl_context=('/home/mazinlab/appcerts/cert.pem', '/home/mazinlab/appcerts/key.pem'))
