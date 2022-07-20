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
