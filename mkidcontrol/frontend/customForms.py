from flask_wtf import FlaskForm
from wtforms import SelectField, SubmitField, StringField
from mkidcontrol.commands import COMMAND_DICT


SIM921_SETTING_KEYS = {'resistancerange': {'key':'device-settings:sim921:resistance-range', 'type': 'sim921', 'prefix': True, 'field_type':'select', 'label':'\u26A0 Resistance Range (\u03A9)'},
              'excitationvalue': {'key':'device-settings:sim921:excitation-value', 'type': 'sim921', 'prefix': True, 'field_type':'select', 'label':'\u26A0 Excitation Value (V)'},
              'excitationmode': {'key':'device-settings:sim921:excitation-mode', 'type': 'sim921', 'prefix': True, 'field_type':'select', 'label':'\u26A0 Excitation Mode'},
              'timeconstant': {'key':'device-settings:sim921:time-constant', 'type': 'sim921', 'prefix': True, 'field_type':'select', 'label':'\u26A0 Time Constant (s)'},
              'tempslope': {'key':'device-settings:sim921:temp-slope', 'type': 'sim921', 'prefix': True, 'field_type':'string', 'label':'\u26A0 Temperature Slope (V/K)'},
              'resistanceslope': {'key':'device-settings:sim921:resistance-slope', 'type': 'sim921', 'prefix': True, 'field_type':'string', 'label':'\u26A0 Resistance Slope (V/\u03A9)'},
              'curve': {'key':'device-settings:sim921:curve-number', 'type': 'sim921', 'prefix': True, 'field_type':'select', 'label':'\u26A0 Calibration Curve'}}

SIM960_SETTING_KEYS = {'voutmin': {'key':'device-settings:sim960:vout-min-limit', 'type':'sim960', 'prefix': True, 'field_type':'string', 'label':'\u26A0 Minimum Output (V)'},
              'voutmax': {'key':'device-settings:sim960:vout-max-limit', 'type':'sim960', 'prefix': True, 'field_type':'string', 'label':'\u26A0 Maximum Output (V)'},
              'vinsetpointmode': {'key':'device-settings:sim960:vin-setpoint-mode', 'type':'sim960', 'prefix': True, 'field_type':'select', 'label':'\u26A0 Input Voltage Mode'},
              'vinsetpointvalue': {'key':'device-settings:sim960:vin-setpoint', 'type':'sim960', 'prefix': True, 'field_type':'string', 'label':'\u26A0 Input Voltage Desired Value(V)'},
              'vinsetpointslewenable': {'key':'device-settings:sim960:vin-setpoint-slew-enable', 'type':'sim960', 'prefix': True, 'field_type':'select', 'label':'\u26A0 Enable Internal Setpoint Slew'},
              'vinsetpointslewrate': {'key':'device-settings:sim960:vin-setpoint-slew-rate', 'type':'sim960', 'prefix': True, 'field_type':'string', 'label':'\u26A0 Internal Setpoint Slew Rate'},
              'pidpval': {'key':'device-settings:sim960:pid-p:value', 'type':'sim960', 'prefix': True, 'field_type':'string', 'label':'\u26A0 PID: P Value'},
              'pidival': {'key':'device-settings:sim960:pid-i:value', 'type':'sim960', 'prefix': True, 'field_type':'string', 'label':'\u26A0 PID: I Value'},
              'piddval': {'key':'device-settings:sim960:pid-d:value', 'type':'sim960', 'prefix': True, 'field_type':'string', 'label':'\u26A0 PID: D Value'},
              'pidoval': {'key':'device-settings:sim960:pid-offset:value', 'type':'sim960', 'prefix': True, 'field_type':'string', 'label':'\u26A0 PID: Offset Value'},
              'pidpenable': {'key':'device-settings:sim960:pid-p:enabled', 'type':'sim960', 'prefix': True, 'field_type':'select', 'label':'\u26A0 PID: Enable P'},
              'pidienable': {'key':'device-settings:sim960:pid-i:enabled', 'type':'sim960', 'prefix': True, 'field_type':'select', 'label':'\u26A0 PID: Enable I'},
              'piddenable': {'key':'device-settings:sim960:pid-d:enabled', 'type':'sim960', 'prefix': True, 'field_type':'select', 'label':'\u26A0 PID: Enable D'},
              'pidoenable': {'key':'device-settings:sim960:pid-offset:enabled', 'type':'sim960', 'prefix': True, 'field_type':'select', 'label':'\u26A0 PID: Enable Offset'}}

HEATSWITCH_SETTING_KEYS = {'open': {'key':'device-settings:currentduino:heatswitch', 'type':'heatswitch', 'prefix': True, 'field_type':'submit', 'label':'Open'},
                           'close': {'key':'device-settings:currentduino:heatswitch', 'type':'heatswitch', 'prefix': True, 'field_type':'submit', 'label':'Close'}}

MAGNET_COMMAND_FORM_KEYS = {'soakcurrent': {'key':'device-settings:sim960:soak-current', 'type':'magnet', 'prefix': False, 'field_type':'string', 'label':'Soak Current (A)'},
                            'soaktime': {'key':'device-settings:sim960:soak-time', 'type':'magnet', 'prefix': False, 'field_type':'string', 'label':'Soak Time (s)'},
                            'ramprate': {'key':'device-settings:sim960:ramp-rate', 'type':'magnet', 'prefix': False, 'field_type':'string', 'label':'Ramp Rate (A/s)'},
                            'deramprate': {'key':'device-settings:sim960:deramp-rate', 'type':'magnet', 'prefix': False, 'field_type':'string', 'label':'Deramp Rate (A/s)'},
                            'regulationtemperature': {'key':'device-settings:mkidarray:regulating-temp', 'type':'magnet', 'prefix': True, 'field_type':'select', 'label':'Regulation Temperature (K)'}}

CYCLE_KEYS = {'startcooldown': {'key':'command:get-cold', 'type': 'cycle', 'prefix': False, 'schedule':False, 'field_type': 'submit', 'label':'Start Cooldown'},
              'abortcooldown': {'key':'command:abort-cooldown', 'type': 'cycle', 'prefix': False, 'schedule':False, 'field_type': 'submit', 'label':'Abort Cooldown'},
              'cancelcooldown': {'key':'command:cancel-scheduled-cooldown', 'type': 'cycle', 'prefix': False, 'schedule':False, 'field_type': 'submit', 'label':'Cancel Scheduled Cooldown'},
              'schedulecooldown': {'key':'command:be-cold-at', 'type': 'cycle', 'prefix': False, 'schedule':True, 'field_type': 'string', 'label':'Schedule Cooldown'}}


FIELD_KEYS = {}
FIELD_KEYS.update(SIM921_SETTING_KEYS)
FIELD_KEYS.update(SIM960_SETTING_KEYS)
FIELD_KEYS.update(HEATSWITCH_SETTING_KEYS)
FIELD_KEYS.update(MAGNET_COMMAND_FORM_KEYS)
FIELD_KEYS.update(CYCLE_KEYS)


def make_select_choices(key):
    """
    Creates a list to use in a flask SelectField. Takes the allowable values from the devices.py COMMAND_DICT
    """
    choices = list(COMMAND_DICT[key]['vals'].keys())
    return choices


def make_field(key):
    field_info = FIELD_KEYS[key]
    field_type = field_info['field_type']
    if field_type == 'submit':
        field = SubmitField(field_info['label'], id=key)
    elif field_type == 'string':
        field = StringField(field_info['label'], id=key)
    elif field_type == 'select':
        field = SelectField(field_info['label'], id=key, choices=make_select_choices(field_info['key']))
    return field


class TestForm(FlaskForm):
    pass


class CycleControlForm(FlaskForm):
    startcooldown = make_field('startcooldown')
    abortcooldown = make_field('abortcooldown')
    cancelcooldown = make_field('cancelcooldown')
    schedulecooldown = make_field('schedulecooldown')


class MagnetControlForm(FlaskForm):
    soakcurrent = make_field('soakcurrent')
    soaktime = make_field('soaktime')
    ramprate = make_field('ramprate')
    deramprate = make_field('deramprate')
    regulationtemperature = make_field('regulationtemperature')


class SIM921SettingForm(FlaskForm):
    title = "SIM 921"
    resistancerange = make_field('resistancerange')
    excitationvalue = make_field('excitationvalue')
    excitationmode = make_field('excitationmode')
    timeconstant = make_field('timeconstant')
    tempslope = make_field('tempslope')
    resistanceslope = make_field('resistanceslope')
    curve = make_field('curve')


class SIM960SettingForm(FlaskForm):
    title = "SIM 960"
    voutmin = make_field('voutmin')
    voutmax = make_field('voutmax')
    vinsetpointmode = make_field('vinsetpointmode')
    vinsetpointvalue = make_field('vinsetpointvalue')
    vinsetpointslewenable = make_field('vinsetpointslewenable')
    vinsetpointslewrate = make_field('vinsetpointslewrate')
    pidpval = make_field('pidpval')
    pidival = make_field('pidival')
    piddval = make_field('piddval')
    pidoval = make_field('pidoval')
    pidpenable = make_field('pidpenable')
    pidienable = make_field('pidienable')
    piddenable = make_field('piddenable')
    pidoenable = make_field('pidoenable')


class HeatswitchToggle(FlaskForm):
    open = make_field('open')
    close = make_field('close')
