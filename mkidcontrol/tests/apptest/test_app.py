import flask
from flask_wtf import FlaskForm
from flask_bootstrap import Bootstrap
from flask import request, redirect, url_for, render_template, jsonify, Response
import time

app = flask.Flask(__name__)
app.logger.setLevel('DEBUG')
app.secret_key = 'heres-a-big-many-charaacter-secret-key'
bootstrap = Bootstrap(app)


@app.route('/listen')
def listen():
    app.logger.debug(f"listening!!")
    return Response(foobar(), mimetype='text/event-stream', content_type='text/event-stream')

def foobar():
    for message in ({'type':'message', 'data':f"{i}".encode('utf-8')} for i in range(11)):
        if message['type'] == 'message':
            msg = f"retry:5\ndata: {message['data'].decode('utf-8')}\n\n"
            app.logger.debug(msg)
            time.sleep(2)
            yield msg
    app.logger.info(f"pubsub ended!")


@app.route('/', methods=['GET', 'POST'])
@app.route('/main', methods=['GET', 'POST'])
def index():
    form = FlaskForm()
    return render_template('index.html', title='Home', form=form)


if __name__ == "__main__":
    app.run(port=8000, threaded=True, debug=True)
