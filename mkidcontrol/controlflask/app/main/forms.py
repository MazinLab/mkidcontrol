from flask import request
from flask_wtf import FlaskForm
import wtforms
from wtforms import StringField, SubmitField, TextAreaField, BooleanField, FormField, FloatField
from wtforms.validators import ValidationError, DataRequired, Length, NumberRange
from flask_babel import _, lazy_gettext as _l

from ....config import MAX_LED_LEVEL


class CloudControl(FlaskForm):
    keepalive = BooleanField('Keep Speaker Connected', description='Untick if the speaker is beeping every 15 minutes')
    thermal_limit = FloatField('Thermal Limit (\N{DEGREE SIGN}F)', description='Brightness limited when above',
                               validators=[NumberRange(120, 180), DataRequired()])
    thermal_brightness = FloatField('Thermal Dimming Multiplier', description='Limit total brightness ',
                                    validators=[NumberRange(0, 1), DataRequired()])
    max_led_level = FloatField('LED Current Limit', description='Total brightness limit',
                               validators=[NumberRange(0, MAX_LED_LEVEL), DataRequired()])
    submit = SubmitField('Update')


class EmptyForm(FlaskForm):
    submit = SubmitField('Submit')
