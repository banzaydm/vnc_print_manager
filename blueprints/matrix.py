# -*- coding: utf-8 -*-
"""Вкладка Matrix: админка Synapse (пользователи, комнаты)."""
from flask import Blueprint, jsonify

from services import matrix_service
from services.api_utils import get_json

bp = Blueprint('matrix', __name__)


def _err(e):
    return jsonify({'success': False, 'error': str(e)}), e.http_code or 502


@bp.route('/api/matrix/status', methods=['GET'])
def matrix_status():
    """Конфигурация Matrix и результат пробного входа администратора."""
    try:
        return jsonify({'success': True, **matrix_service.status()})
    except matrix_service.MatrixError as e:
        return _err(e)


@bp.route('/api/matrix/users', methods=['GET'])
def matrix_users():
    """Пользователи сервера с онлайн-статусом (last_seen_ts)."""
    if not matrix_service.configured():
        return jsonify({'success': False, 'configured': False,
                        'error': 'Matrix не настроен — заполните адрес сервера '
                                 'и данные администратора в настройках.'}), 400
    try:
        return jsonify({'success': True, 'configured': True,
                        **matrix_service.list_users()})
    except matrix_service.MatrixError as e:
        return _err(e)


@bp.route('/api/matrix/rooms', methods=['GET'])
def matrix_rooms():
    """Комнаты сервера."""
    if not matrix_service.configured():
        return jsonify({'success': False, 'configured': False,
                        'error': 'Matrix не настроен.'}), 400
    try:
        return jsonify({'success': True, **matrix_service.list_rooms()})
    except matrix_service.MatrixError as e:
        return _err(e)


@bp.route('/api/matrix/users', methods=['POST'])
def matrix_user_create():
    """Создать пользователя. Пароль можно не указать — будет сгенерирован."""
    data = get_json()
    try:
        res = matrix_service.create_user(
            data.get('localpart'), data.get('password'),
            data.get('displayname'), data.get('admin'))
        return jsonify({'success': True, **res})
    except matrix_service.MatrixError as e:
        return _err(e)


@bp.route('/api/matrix/users/update', methods=['POST'])
def matrix_user_update():
    """Действия над пользователем: password/deactivate/activate/make_admin/revoke_admin."""
    data = get_json()
    user_id = (data.get('user_id') or '').strip()
    action = (data.get('action') or '').strip()
    try:
        matrix_service.update_user(user_id, action, data.get('password'))
        return jsonify({'success': True})
    except matrix_service.MatrixError as e:
        return _err(e)
