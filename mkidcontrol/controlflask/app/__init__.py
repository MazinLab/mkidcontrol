import logging
from flask import Flask, request, current_app
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_login import LoginManager
from flask_mail import Mail
from flask_bootstrap import Bootstrap
from flask_moment import Moment
from flask_babel import Babel, lazy_gettext as _l
import mkidcontrol.mkidredis as redis
from mkidcontrol.util import setup_logging
import threading
import queue
from mkidcontrol.agents.xkid.heatswitchAgent import TS_KEYS as TS_KEYS_hs
from mkidcontrol.agents.lakeshore336Agent import TS_KEYS as TS_KEYS_ls336
from mkidcontrol.agents.lakeshore372Agent import TS_KEYS as TS_KEYS_ls372
from mkidcontrol.agents.lakeshore625Agent import TS_KEYS as TS_KEYS_ls625
from mkidcontrol.agents.xkid.magnetAgent import TS_KEYS as TS_KEYS_magnet
# try:
from ...config import Config
# from ...config import schema_keys
# except ValueError:
#     from config import Config

TS_KEYS = tuple(TS_KEYS_hs) + tuple(TS_KEYS_ls336) + tuple(TS_KEYS_ls372) + tuple(TS_KEYS_ls625) + tuple(TS_KEYS_magnet)

db = SQLAlchemy()
migrate = Migrate()
login = LoginManager()
login.login_view = 'auth.login'
login.login_message = _l('Please log in to access this page.')
mail = Mail()
bootstrap = Bootstrap()
moment = Moment()
babel = Babel()


def event_stream():
    for _, v in current_app.redis.listen('chat'):
        yield f'data: {v}\n\n'


class MessageAnnouncer:
    def __init__(self):
        self.listeners = []

    def listen(self):
        self.listeners.append(queue.Queue(maxsize=5))
        return self.listeners[-1]

    def announce(self, msg):
        # We go in reverse order because we might have to delete an element, which will shift the
        # indices backward
        # getLogger(__name__).info(f'Announcing {msg}')
        for i in reversed(range(len(self.listeners))):
            try:
                self.listeners[i].put_nowait(msg)
            except queue.Full:
                del self.listeners[i]


def datagen(redis, announcer):
    import json
    for k, v in redis.listen(schema_keys()):

        event = 'update'
        data = {k:v}

        # plotid = 'temp:value'
        # since = None
        # kind = 'full' if since is None else 'partial'
        # new = list(zip(*redis.range(plotid, since)))
        # data = {'id': f'redisplot:{plotid}', 'kind': kind, 'data': {'x': new[0], 'y': new[1]}}

        announcer.announce(f"event:{event}\nretry:5\ndata: {json.dumps(data)}\n\n")

    datalistener = threading.Thread(target=datagen, args=(app.redis, app.announcer), daemon=True)
    datalistener.start()


def create_app(config_class=Config):
    # TODO: Login db stuff and mail stuff can reasonably go
    app = Flask(__name__)
    app.config.from_object(config_class)

    db.init_app(app)
    migrate.init_app(app, db)
    login.init_app(app)
    mail.init_app(app)
    bootstrap.init_app(app)
    moment.init_app(app)
    babel.init_app(app)
    redis.setup_redis(ts_keys=TS_KEYS)
    app.redis = redis.mkidredis #Redis.from_url(app.config['REDIS_URL'])
    # app.task_queue = rq.Queue('mkidcontrol', connection=app.redis.redis)
    # app.scheduler = rq_scheduler.Scheduler('mkidcontrol', connection=app.redis.redis)
    # app.announcer = MessageAnnouncer()
    # datalistener = threading.Thread(target=datagen, args=(app.redis, app.announcer), daemon=True)
    # datalistener.start()

    from .errors import bp as errors_bp
    app.register_blueprint(errors_bp)

    from .auth import bp as auth_bp
    app.register_blueprint(auth_bp, url_prefix='/auth')

    from .main import bp as main_bp
    app.register_blueprint(main_bp)

    # TODO: Axe if not used, a half finished feature is worse than not having it at all. If we don't need
    from .api import bp as api_bp
    app.register_blueprint(api_bp, url_prefix='/api')

    if not app.debug and not app.testing:
        # if app.config['MAIL_SERVER']:
        #     auth = None
        #     if app.config['MAIL_USERNAME'] or app.config['MAIL_PASSWORD']:
        #         auth = (app.config['MAIL_USERNAME'],
        #                 app.config['MAIL_PASSWORD'])
        #     secure = None
        #     if app.config['MAIL_USE_TLS']:
        #         secure = ()
        #     mail_handler = SMTPHandler(
        #         mailhost=(app.config['MAIL_SERVER'], app.config['MAIL_PORT']),
        #         fromaddr='no-reply@' + app.config['MAIL_SERVER'],
        #         toaddrs=app.config['ADMINS'], subject='Cloudlight Failure',
        #         credentials=auth, secure=secure)
        #     mail_handler.setLevel(logging.ERROR)
        #     app.logger.addHandler(mail_handler)

        if app.config['LOG_TO_STDOUT']:
            stream_handler = logging.StreamHandler()
            stream_handler.setLevel(logging.INFO)
            app.logger.addHandler(stream_handler)
        else:
            setup_logging('controlDirector')

        app.logger.info('MKID Control startup')

    with app.app_context():
        db.create_all()

    return app


@babel.localeselector
def get_locale():
    return request.accept_languages.best_match(current_app.config['LANGUAGES'])


from . import models
# try:
#     from ..app import models
# except:
#     from app import models
