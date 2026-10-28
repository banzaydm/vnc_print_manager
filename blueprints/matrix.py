# -*- coding: utf-8 -*-
"""Вкладка Matrix: админка Synapse (пользователи, комнаты)."""
from flask import Blueprint, jsonify, request, Response

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


@bp.route('/api/matrix/rooms', methods=['POST'])
def matrix_room_create():
    """Создать комнату: {name, topic?, invite?}. invite — список user_id."""
    data = get_json()
    try:
        res = matrix_service.create_room(
            data.get('name'), data.get('topic'), data.get('invite'))
        return jsonify({'success': True, **res})
    except matrix_service.MatrixError as e:
        return _err(e)


@bp.route('/api/matrix/rooms/members', methods=['GET'])
def matrix_room_members():
    """Участники комнаты (joined + invited) с ролями: ?room_id=!xxx:hs."""
    from flask import request
    room_id = (request.args.get('room_id') or '').strip()
    if not room_id:
        return jsonify({'success': False,
                        'error': 'Не передан room_id'}), 400
    try:
        return jsonify({'success': True,
                        **matrix_service.get_room_members(room_id)})
    except matrix_service.MatrixError as e:
        return _err(e)


@bp.route('/api/matrix/rooms/invite', methods=['POST'])
def matrix_room_invite():
    """Пригласить пользователя: {room_id, user_id}."""
    data = get_json()
    try:
        matrix_service.invite_user(data.get('room_id'), data.get('user_id'))
        return jsonify({'success': True})
    except matrix_service.MatrixError as e:
        return _err(e)


@bp.route('/api/matrix/rooms/kick', methods=['POST'])
def matrix_room_kick():
    """Исключить пользователя: {room_id, user_id, reason?}."""
    data = get_json()
    try:
        matrix_service.kick_user(data.get('room_id'), data.get('user_id'),
                                 data.get('reason'))
        return jsonify({'success': True})
    except matrix_service.MatrixError as e:
        return _err(e)


@bp.route('/api/matrix/rooms/join', methods=['POST'])
def matrix_room_join():
    """Админ входит в комнату: {room_id} (для управления ролями/составом)."""
    data = get_json()
    try:
        matrix_service.join_room(data.get('room_id'))
        return jsonify({'success': True})
    except matrix_service.MatrixError as e:
        return _err(e)


@bp.route('/api/matrix/rooms/power-level', methods=['POST'])
def matrix_room_power_level():
    """Изменить роль: {room_id, user_id, level: 0|50|100}."""
    data = get_json()
    try:
        matrix_service.set_power_level(data.get('room_id'),
                                       data.get('user_id'),
                                       data.get('level'))
        return jsonify({'success': True})
    except matrix_service.MatrixError as e:
        return _err(e)


@bp.route('/api/matrix/rooms/delete', methods=['POST'])
def matrix_room_delete():
    """Удалить комнату: {room_id, reason?}."""
    data = get_json()
    try:
        matrix_service.delete_room(data.get('room_id'), data.get('reason'))
        return jsonify({'success': True})
    except matrix_service.MatrixError as e:
        return _err(e)


@bp.route('/api/matrix/rooms/update', methods=['POST'])
def matrix_room_update():
    """Изменить комнату: {room_id, name?, topic?, avatar_url?}."""
    data = get_json()
    try:
        matrix_service.update_room(data.get('room_id'), data.get('name'),
                                   data.get('topic'), data.get('avatar_url'))
        return jsonify({'success': True})
    except matrix_service.MatrixError as e:
        return _err(e)


@bp.route('/api/matrix/media/upload', methods=['POST'])
def matrix_media_upload():
    """Загрузить изображение: {data: base64, content_type} → {mxc_url}."""
    import base64 as b64mod
    data = get_json()
    try:
        raw = b64mod.b64decode(data.get('data') or '')
    except Exception:
        return jsonify({'success': False, 'error': 'Некорректный base64'}), 400
    if not raw:
        return jsonify({'success': False, 'error': 'Пустой файл'}), 400
    if len(raw) > 5 * 1024 * 1024:
        return jsonify({'success': False, 'error': 'Файл больше 5 МБ'}), 400
    ctype = (data.get('content_type') or 'application/octet-stream') \
        .split(';')[0].strip()
    if not ctype.startswith('image/'):
        return jsonify({'success': False, 'error': 'Только изображения'}), 400
    try:
        mxc = matrix_service.upload_media(raw, ctype)
        return jsonify({'success': True, 'mxc_url': mxc,
                        'http_url': matrix_service.mxc_to_http(mxc, 64, 64)})
    except matrix_service.MatrixError as e:
        return _err(e)


@bp.route('/api/matrix/media/<server>/<media_id>', methods=['GET'])
def matrix_media_proxy(server, media_id):
    """Прокси медиа Synapse для <img src> (иначе 401: media требует токен).

    ?width=&height= — thumbnail (scale), без них — полный файл.
    """
    width = request.args.get('width', type=int)
    height = request.args.get('height', type=int)
    try:
        data, ctype = matrix_service.fetch_media(server, media_id,
                                                 width, height)
        return Response(data, content_type=ctype,
                        headers={'Cache-Control': 'private, max-age=86400'})
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
    """Действия над пользователем: password/deactivate/activate/make_admin/revoke_admin/profile."""
    data = get_json()
    user_id = (data.get('user_id') or '').strip()
    action = (data.get('action') or '').strip()
    try:
        matrix_service.update_user(
            user_id, action, data.get('password'),
            data.get('displayname'), data.get('admin'),
            data.get('avatar_url'))
        return jsonify({'success': True})
    except matrix_service.MatrixError as e:
        return _err(e)


@bp.route('/api/matrix/broadcast', methods=['POST'])
def matrix_broadcast():
    """Рассылка сообщения: mode=dm (личные) или mode=room (общий рум).

    Для mode=room можно передать room_id — тогда сообщение уйдёт именно
    в эту комнату (без автоинвайта). Без room_id — старое поведение
    «Рассылка VNC Manager».
    """
    if not matrix_service.configured():
        return jsonify({'success': False, 'configured': False,
                        'error': 'Matrix не настроен.'}), 400
    data = get_json()
    try:
        res = matrix_service.send_broadcast(
            data.get('message'), data.get('mode') or 'dm',
            data.get('users'), data.get('room_id'))
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
