from flask import request
from flask_wtf import FlaskForm
import wtforms
from wtforms import StringField, SubmitField, TextAreaField, BooleanField, FormField, FloatField
from wtforms.validators import ValidationError, DataRequired, Length, NumberRange
from flask_babel import _, lazy_gettext as _l

import wtforms
from wtforms.fields import *
from wtforms.widgets import HiddenInput
from wtforms.fields.html5 import *
from wtforms.validators import *
from wtforms import Form
from flask_wtf import FlaskForm
from serial import SerialException


class EmptyForm(FlaskForm):
    submit = SubmitField('Submit')


class LS336Form(FlaskForm):
    title = "Lake Shore 336 Settings"
    set = SubmitField("Set Lake Shore")


class InputSensorForm(FlaskForm):
    from ....commands import LS336_INPUT_SENSOR_TYPES, LS336_INPUT_SENSOR_UNITS, LS336_AUTORANGE_VALUES, \
        LS336_COMPENSATION_VALUES
    channel = HiddenField("")
    name = StringField("Name")
    sensor_type = SelectField("Sensor Type", choices=list(LS336_INPUT_SENSOR_TYPES.keys()))
    units = SelectField("Units", choices=list(LS336_INPUT_SENSOR_UNITS.keys()))
    curve = SelectField("Curve", choices=np.arange(1, 60))
    autorange = SelectField(label="Autorange Enable", choices=list(LS336_AUTORANGE_VALUES.keys()))
    compensation = SelectField(label="Compensation", choices=list(LS336_COMPENSATION_VALUES.keys()))


class DiodeForm(InputSensorForm):
    from ....commands import LS336_DIODE_RANGE
    input_range = SelectField("Input Range", choices=list(LS336_DIODE_RANGE.keys()))
    update = SubmitField("Update")


class RTDForm(InputSensorForm):
    from ....commands import LS336_RTD_RANGE
    input_range = SelectField("Input Range", choices=list(LS336_RTD_RANGE.keys()))
    update = SubmitField("Update")


class DisabledInputForm(FlaskForm):
    from ....commands import LS336_INPUT_SENSOR_TYPES, LS336_INPUT_SENSOR_UNITS, LS336_INPUT_SENSOR_RANGE
    channel = HiddenField()
    name = StringField("Name")
    sensor_type = SelectField("Sensor Type", choices=list(LS336_INPUT_SENSOR_TYPES.keys()))
    units = SelectField("Units", choices=list(LS336_INPUT_SENSOR_UNITS.keys()), render_kw={'disabled':True})
    curve = SelectField("Curve", choices=np.arange(1, 60), render_kw={'disabled':True})
    autorange = BooleanField(label="Autorange Enable", render_kw={'disabled':True})
    compensation = BooleanField(label="Compensation", render_kw={'disabled':True})
    input_range = SelectField("Input Range", choices=list(LS336_INPUT_SENSOR_RANGE.keys()), render_kw={'disabled':True})
    enable = SubmitField("Enable")


class Schedule(FlaskForm):
    at = DateTimeLocalField('Activate at', format='%m/%d/%Y %I:%M %p')
    # at = wtforms.DateTimeField('Activate at', format='%m/%d/%Y %I:%M %p')
    # date = wtforms.fields.html5.DateField('Date')
    # time = wtforms.fields.html5.TimeField('Time')
    repeat = BooleanField(label='Every Day?', default=True)
    clear = SubmitField("Clear")
    schedule = SubmitField("Set")