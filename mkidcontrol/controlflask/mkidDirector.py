"""
TODO: Condense/make more sensible validation handling
TODO: Try to remove hardcoding as best as possible

TODO: Integrate Jeb's flask changes
TODO: Incorporate power on/off into the monitoring/control panel
 - Related: Fix errors if redis range is empty (i.e. no values reported since the device was off)

TODO (FOR ALL DEVICES): Enable graceful power on/off handling (i.e. don't error out if device is purposely switched off)
"""
from .app import create_app, db, cli
from .app.models import User, Post, Message, Notification, Task
from mkidcontrol.util import setup_logging

log = setup_logging('controlDirector')

app = create_app()
cli.register(app)

@app.shell_context_processor
def make_shell_context():
    return {'db': db, 'User': User, 'Post': Post, 'Message': Message,
            'Notification': Notification, 'Task': Task}

# import flask
# from flask_wtf import FlaskForm
# from flask_bootstrap import Bootstrap
# from flask import request, render_template, jsonify, Response
# import numpy as np
# import time, datetime
# import json
# import plotly
# import subprocess
# import select
#
# from mkidcontrol.controlflask.config import Config
# import mkidcontrol.mkidredis as redis
# from mkidcontrol.commands import COMMAND_DICT, SimCommand
#
# from mkidcontrol.sim960Agent import SIM960_KEYS
# from mkidcontrol.sim921Agent import SIM921_KEYS
# from mkidcontrol.lakeshore240Agent import LAKESHORE240_KEYS
# from mkidcontrol.hemttempAgent import HEMTTEMP_KEYS
# from mkidcontrol.currentduinoAgent import CURRENTDUINO_KEYS
# from mkidcontrol.controlflask.app.main.customForms import CycleControlForm, MagnetControlForm, SIM921SettingForm, \
#     SIM960SettingForm, HeatswitchToggle, TestForm, FIELD_KEYS

# app = flask.Flask(__name__)
# app.logger.setLevel('DEBUG')
# bootstrap = Bootstrap(app)
# app.config.from_object(Config)

# if __name__ == "__main__":
#     redis.setup_redis(create_ts_keys=TS_KEYS)
#     app.run(port=8000, threaded=True, debug=True, ssl_context=('/home/mazinlab/appcerts/cert.pem', '/home/mazinlab/appcerts/key.pem'))
