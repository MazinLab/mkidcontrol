from app import create_app, db
from .app import cli
from mkidcontrol.controlflask.app.models import User, Post, Message, Notification, Task
from ..util import setup_logging

log = setup_logging('cloud-flask')

app = create_app()
cli.register(app)

@app.shell_context_processor
def make_shell_context():
    return {'db': db, 'User': User, 'Post': Post, 'Message': Message,
            'Notification': Notification, 'Task': Task}
