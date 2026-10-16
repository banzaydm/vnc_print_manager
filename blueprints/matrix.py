# -*- coding: utf-8 -*-
"""Вкладка Matrix: админка Synapse (пользователи, комнаты)."""
from flask import Blueprint, jsonify

from services import matrix_groups, matrix_service
from services.api_utils import get_json

bp = Blueprint('matrix', __name__)


def _err(e):
    return jsonify({'success': False, 'error': str(e)}), e.http_code or 502


def _group_err(e):
    return jsonify({'success': False, 'error': str(e)}), e.http_code or 400


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


@bp.route('/api/matrix/broadcast', methods=['POST'])
def matrix_broadcast():
    """Рассылка сообщения: mode=dm (личные) или mode=room (общий рум)."""
    if not matrix_service.configured():
        return jsonify({'success': False, 'configured': False,
                        'error': 'Matrix не настроен.'}), 400
    data = get_json()
    try:
        res = matrix_service.send_broadcast(
            data.get('message'), data.get('mode') or 'dm',
            data.get('users'))
        return jsonify({'success': True, **res})
    except matrix_service.MatrixError as e:
        return _err(e)


@bp.route('/api/matrix/groups', methods=['GET'])
def matrix_groups_list():
    """Локальные группы пользователей (фильтр вкладки)."""
    try:
        return jsonify({'success': True, 'groups': matrix_groups.list_groups()})
    except matrix_groups.GroupError as e:
        return _group_err(e)


@bp.route('/api/matrix/groups', methods=['POST'])
def matrix_group_create():
    """Создать группу: {name}."""
    data = get_json()
    try:
        res = matrix_groups.create_group(data.get('name'))
        return jsonify({'success': True, **res})
    except matrix_groups.GroupError as e:
        return _group_err(e)


@bp.route('/api/matrix/groups/update', methods=['POST'])
def matrix_group_update():
    """Переименовать группу: {id, name}."""
    data = get_json()
    try:
        res = matrix_groups.rename_group(data.get('id'), data.get('name'))
        return jsonify({'success': True, **res})
    except matrix_groups.GroupError as e:
        return _group_err(e)


@bp.route('/api/matrix/groups/<int:group_id>', methods=['DELETE'])
def matrix_group_delete(group_id):
    """Удалить группу (участники освобождаются)."""
    try:
        matrix_groups.delete_group(group_id)
        return jsonify({'success': True})
    except matrix_groups.GroupError as e:
        return _group_err(e)


@bp.route('/api/matrix/groups/assign', methods=['POST'])
def matrix_group_assign():
    """Назначить пользователя в группу: {user_id, group_id|null}."""
    data = get_json()
    try:
        res = matrix_groups.assign(data.get('user_id'), data.get('group_id'))
        return jsonify({'success': True, **res})
    except matrix_groups.GroupError as e:
        return _group_err(e)


@bp.route('/api/matrix/hidden', methods=['GET'])
def matrix_hidden_list():
    """Скрытые («удалённые из списка») пользователи."""
    try:
        return jsonify({'success': True, 'hidden': matrix_groups.list_hidden()})
    except matrix_groups.GroupError as e:
        return _group_err(e)


@bp.route('/api/matrix/users/hide', methods=['POST'])
def matrix_user_hide():
    """Скрыть/показать пользователя: {user_id, hidden: bool}."""
    data = get_json()
    try:
        res = matrix_groups.set_hidden(data.get('user_id'),
                                       bool(data.get('hidden')))
        return jsonify({'success': True, **res})
    except matrix_groups.GroupError as e:
        return _group_err(e)
