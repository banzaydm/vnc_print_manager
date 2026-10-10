# -*- coding: utf-8 -*-
"""Клиент админ-API Synapse (Matrix): логин админа, кэш токена, пользователи
с онлайн-статусом, комнаты, создание и управление пользователями.

Онлайн определяется по полю last_seen_ts из /_synapse/admin/v2/users
(клиентский presence API админу недоступен — 403 M_FORBIDDEN).
"""
import json
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from services.settings_store import _get_setting

ONLINE_WINDOW = 180          # сек: «онлайн», если last_seen_ts свежее
DEFAULT_HS_URL = 'http://192.168.17.250:8008'
USERS_PAGE = 200
LOCALPART_RE = re.compile(r'^[a-z0-9._=/-]{1,64}$')
BROADCAST_ROOM_NAME = 'Рассылка VNC Manager'
_SEND_DELAY = 0.2             # пауза между личными сообщениями (анти-рателIMIT)
_INVITE_DELAY = 0.1

_lock = threading.Lock()
_tokens = {}  # (hs_url, admin_user) -> (token, server_name, expires_at)


class MatrixError(Exception):
    """Ошибка Matrix-сервера или конфигурации. http_code — статус для клиента."""

    def __init__(self, message, http_code=None):
        super().__init__(message)
        self.http_code = http_code


def _cfg():
    hs = (_get_setting('matrix_hs_url', '') or '').strip().rstrip('/')
    admin = (_get_setting('matrix_admin_user', '') or '').strip()
    if admin and not admin.startswith('@'):
        admin = '@' + admin
    password = _get_setting('matrix_admin_pass', '') or ''
    return hs or DEFAULT_HS_URL, admin, password


def configured():
    _, admin, password = _cfg()
    return bool(admin and password)


def _http(hs_url, path, method='GET', body=None, token=None, timeout=10):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(hs_url + path, data=data, headers=headers,
                                 method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        raw = resp.read()
        return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read() or b'{}'
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {'errcode': 'M_UNKNOWN',
                            'error': raw[:200].decode('utf-8', 'replace')}
    except Exception as e:
        return None, {'errcode': 'M_UNREACHABLE', 'error': str(e)[:200]}


def _login(hs_url, admin_user, password):
    st, out = _http(hs_url, '/_matrix/client/v3/login', 'POST', {
        'type': 'm.login.password',
        'identifier': {'type': 'm.id.user', 'user': admin_user.lstrip('@')},
        'password': password,
        'device_id': 'vnc-manager',
    })
    if st != 200 or not out.get('access_token'):
        raise MatrixError(
            f'Не удалось войти в Matrix ({admin_user}): '
            f'{out.get("error") or ("HTTP " + str(st))}', 401)
    user_id = out.get('user_id') or admin_user
    server_name = user_id.split(':', 1)[1] if ':' in user_id else ''
    return out['access_token'], server_name


def _drop_token(hs_url, admin_user):
    with _lock:
        _tokens.pop((hs_url, admin_user), None)


def _ensure_token():
    """(token, server_name, hs_url). Кэш на 12 часов, логин при отсутствии."""
    hs_url, admin_user, password = _cfg()
    if not admin_user or not password:
        raise MatrixError('Matrix не настроен: заполните адрес сервера, '
                          'логин и пароль администратора в настройках', 400)
    key = (hs_url, admin_user)
    with _lock:
        cached = _tokens.get(key)
    if cached and cached[2] > time.time():
        return cached[0], cached[1], hs_url
    token, server_name = _login(hs_url, admin_user, password)
    with _lock:
        _tokens[key] = (token, server_name, time.time() + 12 * 3600)
    return token, server_name, hs_url


def _request(path, method='GET', body=None, _retry=True):
    token, server_name, hs_url = _ensure_token()
    st, out = _http(hs_url, path, method=method, body=body, token=token)
    if st is None:
        raise MatrixError(f'Нет связи с Matrix: {out.get("error")}', 502)
    if st == 401 and _retry:
        _drop_token(hs_url, _cfg()[1])
        return _request(path, method, body, _retry=False)
    if st >= 400 or out.get('errcode'):
        msg = out.get('error') or f'HTTP {st}'
        raise MatrixError(f'{msg} ({out.get("errcode") or st})',
                          st if st >= 400 else 400)
    return out, server_name


def _enc_user(user_id):
    return urllib.parse.quote(user_id, safe='')


def _ms(ts):
    """Synapse в списке отдаёт ms, в деталях — сек; нормализуем к ms."""
    ts = int(ts or 0)
    return ts * 1000 if 0 < ts < 10 ** 12 else ts


def status():
    """Конфигурация + пробный логин (при настроенных кредах)."""
    hs_url, admin_user, _password = _cfg()
    info = {
        'configured': configured(),
        'hs_url': hs_url,
        'admin_user': admin_user,
        'server_name': '',
    }
    if not info['configured']:
        return info
    try:
        _token, server_name, _ = _ensure_token()
        info['server_name'] = server_name
    except MatrixError as e:
        info['auth_error'] = str(e)
    return info


def list_users():
    """Пользователи сервера: online (last_seen_ts), активность за 24ч, сортировка."""
    users = []
    offset = 0
    total = None
    server_name = ''
    while True:
        # deactivated=true/guests=true — иначе Synapse молча скрывает
        # деактивированных пользователей из списка
        out, server_name = _request(
            f'/_synapse/admin/v2/users?from={offset}&limit={USERS_PAGE}'
            '&order_by=name&deactivated=true&guests=true')
        batch = out.get('users') or []
        total = out.get('total', total)
        users.extend(batch)
        offset += len(batch)
        if not batch or offset >= 1000 or (total is not None and offset >= total):
            break
    now_ms = int(time.time() * 1000)
    result = []
    for u in users:
        last_ms = _ms(u.get('last_seen_ts'))
        online = bool(last_ms) and (now_ms - last_ms) < ONLINE_WINDOW * 1000
        result.append({
            'user_id': u.get('name') or '',
            'displayname': u.get('displayname') or '',
            'admin': bool(u.get('admin')),
            'deactivated': bool(u.get('deactivated')),
            'locked': bool(u.get('locked')),
            'shadow_banned': bool(u.get('shadow_banned')),
            'creation_ts': _ms(u.get('creation_ts')),
            'last_seen_ts': last_ms,
            'online': online,
            'active_24h': bool(last_ms) and (now_ms - last_ms) < 86400 * 1000,
        })
    result.sort(key=lambda x: (-(x['last_seen_ts'] or 0), x['user_id']))
    return {
        'users': result,
        'total': total if total is not None else len(result),
        'online': sum(1 for x in result if x['online']),
        'active_24h': sum(1 for x in result if x['active_24h']),
        'server_name': server_name,
    }


def create_user(localpart, password=None, displayname='', admin=False):
    """Создать пользователя (PUT /_synapse/admin/v2/users/{id}).

    Возвращает user_id и пароль (сгенерированный — тоже).
    """
    localpart = (localpart or '').strip().lower()
    if not LOCALPART_RE.match(localpart or ''):
        raise MatrixError('Логин: строчные латинские буквы, цифры, . _ - = , '
                          'до 64 символов', 400)
    password = (password or '').strip() or secrets.token_urlsafe(12)
    if len(password) < 8:
        raise MatrixError('Пароль должен быть не короче 8 символов', 400)
    _token, server_name, hs_url = _ensure_token()
    user_id = f'@{localpart}:{server_name}'
    enc = _enc_user(user_id)
    st, _info = _http(hs_url, '/_synapse/admin/v2/users/' + enc,
                      token=_token)
    if st == 200:
        raise MatrixError(f'Пользователь {user_id} уже существует', 409)
    _request('/_synapse/admin/v2/users/' + enc, 'PUT', {
        'password': password,
        'displayname': (displayname or '').strip() or localpart,
        'admin': bool(admin),
        'reason': 'Создан из VNC Manager',
    })
    return {'user_id': user_id, 'password': password}


def update_user(user_id, action, password=None):
    """Действия: password | deactivate | activate | make_admin | revoke_admin."""
    if not user_id or not user_id.startswith('@'):
        raise MatrixError('Некорректный user_id', 400)
    if action == 'password':
        password = (password or '').strip()
        if len(password) < 8:
            raise MatrixError('Пароль должен быть не короче 8 символов', 400)
        body = {'password': password}
    elif action == 'deactivate':
        body = {'deactivated': True}
    elif action == 'activate':
        body = {'deactivated': False}
    elif action == 'make_admin':
        body = {'admin': True}
    elif action == 'revoke_admin':
        body = {'admin': False}
    else:
        raise MatrixError(f'Неизвестное действие: {action}', 400)
    _request('/_synapse/admin/v2/users/' + _enc_user(user_id), 'PUT', body)
    return True


def list_rooms(limit=100):
    """Комнаты сервера (админ-API v1)."""
    out, server_name = _request(
        f'/_synapse/admin/v1/rooms?limit={int(limit)}&order_by=name')
    rooms = []
    for r in out.get('rooms') or []:
        rooms.append({
            'room_id': r.get('room_id') or '',
            'name': r.get('name') or '',
            'topic': r.get('topic') or '',
            'members': int(r.get('num_joined_members') or 0),
            'public': bool(r.get('public')),
            'federatable': bool(r.get('federatable')),
            'creator': r.get('creator') or '',
        })
    rooms.sort(key=lambda x: (-x['members'], x['name'] or x['room_id']))
    return {'rooms': rooms, 'total': out.get('total_rooms', len(rooms)),
            'server_name': server_name}


# ---------------------------------------------------------------------------
# Рассылка сообщений (вкладка Matrix): личные сообщения (DM) или общий рум
# ---------------------------------------------------------------------------
def _me_user_id(server_name):
    admin_user = _cfg()[1]
    if not server_name or not admin_user:
        return admin_user
    return admin_user if ':' in admin_user else f'{admin_user}:{server_name}'


def _send_msg(room_id, text):
    """Отправить m.room.message в комнату.

    PUT (а не POST): на части сборок Synapse POST к /send/ отдаёт 405
    M_UNRECOGNIZED; PUT — канонический метод по спеке (его использует Element)
    и даёт идемпотентность по transaction id.
    """
    txn = secrets.token_hex(8)
    _request(f'/_matrix/client/v3/rooms/{_enc_user(room_id)}'
             f'/send/m.room.message/{txn}', 'PUT',
             {'msgtype': 'm.text', 'body': text})


def _m_direct_path():
    _token, server_name, _hs = _ensure_token()
    return (f'/_matrix/client/v3/user/'
            f'{_enc_user(_me_user_id(server_name))}/account_data/m.direct')


def _get_m_direct():
    """Книга DM: {user_id: [room_id, ...]} из account_data m.direct."""
    out, _ = _request(_m_direct_path())
    return out if isinstance(out, dict) else {}


def _create_dm_room(other):
    out, _ = _request('/_matrix/client/v3/createRoom', 'POST',
                      {'invite': [other], 'preset': 'trusted_private_chat',
                       'is_direct': True})
    room_id = out.get('room_id')
    if not room_id:
        raise MatrixError('Synapse не вернул room_id при создании DM', 502)
    return room_id


def _joined_members(room_id):
    out, _ = _request(
        f'/_matrix/client/v3/rooms/{_enc_user(room_id)}/joined_members')
    return set((out.get('joined') or {}).keys())


def _invited_members(room_id):
    """Приглашённые (но ещё не вошедшие) — им не шлём повторный invite.

    invited_members на этой сборке нет (404) — берём из /members.
    """
    out, _ = _request(
        f'/_matrix/client/v3/rooms/{_enc_user(room_id)}'
        f'/members?membership=invite')
    return {ev.get('state_key') for ev in out.get('chunk') or []
            if ev.get('state_key')}


def _find_broadcast_room(me):
    """Комната рассылки по имени (создана этим же админом), если уже есть."""
    data = list_rooms(limit=200)
    for r in data['rooms']:
        if (r['name'] == BROADCAST_ROOM_NAME
                and r['creator'] in (me, '', None)):
            return r['room_id']
    return None


def send_broadcast(message, mode='dm', user_ids=None):
    """Рассылка выбранным пользователям.

    mode='dm'  — личное сообщение каждому (комната DM создаётся при
                  необходимости и запоминается в m.direct);
    mode='room' — одно сообщение в общий рум «Рассылка VNC Manager»
                  (необходимые участники приглашаются автоматически).
    user_ids=[]/None — все активные пользователи сервера.
    """
    message = (message or '').strip()
    if not message:
        raise MatrixError('Сообщение не может быть пустым', 400)
    if mode not in ('dm', 'room'):
        raise MatrixError(f'Неизвестный режим рассылки: {mode}', 400)

    if not user_ids:
        listed = list_users()
        user_ids = [u['user_id'] for u in listed['users']
                    if not u['deactivated']]
    if isinstance(user_ids, str):
        user_ids = [user_ids]
    user_ids = [u.strip() for u in (user_ids or []) if isinstance(u, str)]
    user_ids = list(dict.fromkeys(u for u in user_ids if u.startswith('@')))
    if not user_ids:
        raise MatrixError('Нет получателей для рассылки', 400)

    _token, server_name, _hs = _ensure_token()
    me = _me_user_id(server_name)
    errors = []

    if mode == 'dm':
        direct = _get_m_direct()
        sent = 0
        skipped = 0
        changed = False
        for uid in user_ids:
            if uid == me:
                skipped += 1      # самому себе личка не создаётся
                continue
            try:
                rooms = direct.get(uid) or []
                try:
                    if not rooms:
                        raise MatrixError('DM ещё не создан')
                    _send_msg(rooms[0], message)
                except MatrixError:
                    room = _create_dm_room(uid)
                    direct[uid] = [room] + [r for r in rooms if r != room]
                    changed = True
                    _send_msg(room, message)
                sent += 1
            except MatrixError as e:
                errors.append({'user': uid, 'error': str(e)})
            time.sleep(_SEND_DELAY)
        if changed:
            try:
                _request(_m_direct_path(), 'PUT', direct)
            except MatrixError:
                pass  # книга m.direct вспомогательная: сообщения уже ушли
        return {'mode': 'dm', 'total': len(user_ids), 'sent': sent,
                'failed': len(errors), 'skipped': skipped,
                'errors': errors[:20]}

    # mode == 'room'
    created = False
    room_id = _find_broadcast_room(me)
    if not room_id:
        out, _ = _request('/_matrix/client/v3/createRoom', 'POST',
                          {'name': BROADCAST_ROOM_NAME,
                           'preset': 'private_chat',
                           'visibility': 'private'})
        room_id = out.get('room_id')
        if not room_id:
            raise MatrixError('Не удалось создать комнату рассылки', 502)
        created = True
    members = _joined_members(room_id) | _invited_members(room_id)
    invited = 0
    for uid in user_ids:
        if uid == me or uid in members:
            continue
        try:
            _request(f'/_matrix/client/v3/rooms/{_enc_user(room_id)}/invite',
                     'POST', {'user_id': uid})
            invited += 1
        except MatrixError as e:
            errors.append({'user': uid, 'error': str(e)})
        time.sleep(_INVITE_DELAY)
    _send_msg(room_id, message)
    return {'mode': 'room', 'room_id': room_id, 'room_name': BROADCAST_ROOM_NAME,
            'created': created, 'total': len(user_ids), 'invited': invited,
            'sent': 1, 'failed': len(errors), 'errors': errors[:20]}
