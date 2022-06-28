from flask import jsonify, request, current_app
from . import bp
from .auth import token_auth
from .errors import bad_request


@bp.route('/redis', methods=['POST', 'GET'])
@token_auth.login_required
def redis_control():
    print('hi')
    if request.method == 'POST':
        try:
            current_app.redis.store(request.form['source'], request.form['value'])
            return jsonify({'success': True})
        except:
            current_app.logger.error('post error', exc_info=True)
            pass
    else:
        try:
            return jsonify({'value': current_app.redis.read(request.args[0])})
        except:
            current_app.logger.error(f'get error {request.args}', exc_info=True)
            pass
    return bad_request('control failed')
