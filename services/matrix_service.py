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


def mxc_to_http(mxc_url, width=None, height=None):
    """mxc://server/media_id → URL прокси нашего приложения для <img src>.

    Synapse на этой сборке отдаёт медиа только через
    /_matrix/client/v1/media/* с Authorization-заголовком — браузер
    напрямую картинку не скачает, поэтому гоняем через /api/matrix/media/.
    """
    if not mxc_url or not isinstance(mxc_url, str) or not mxc_url.startswith('mxc://'):
        return ''
    rest = mxc_url[6:]
    path = f'/api/matrix/media/{rest}'
    if width and height:
        path += f'?width={int(width)}&height={int(height)}'
    return path


def fetch_media(server, media_id, width=None, height=None):
    """Скачать медиафайл с Synapse (client v1 media API, требует токен).

    Возвращает (bytes, content_type).
    """
    token, _, hs_url = _ensure_token()
    if width and height:
        path = (f'/_matrix/client/v1/media/thumbnail/{server}/{media_id}'
                f'?width={int(width)}&height={int(height)}&method=scale')
    else:
        path = f'/_matrix/client/v1/media/download/{server}/{media_id}'
    req = urllib.request.Request(hs_url + path,
                                 headers={'Authorization': 'Bearer ' + token})
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        data = resp.read()
        ctype = resp.headers.get('Content-Type') or 'application/octet-stream'
        return data, ctype
    except urllib.error.HTTPError as e:
        raw = e.read() or b'{}'
        try:
            msg = json.loads(raw).get('error')
        except ValueError:
            msg = raw[:200].decode('utf-8', 'replace')
        raise MatrixError(msg or f'HTTP {e.code}',
                          e.code if e.code >= 400 else 502)
    except Exception as e:
        raise MatrixError(f'Нет связи с media-сервером: {e}', 502)


def upload_media(data, content_type='application/octet-stream'):
    """Загрузить файл в media repo Synapse (POST /_matrix/media/v3/upload).

    data — сырые байты файла. Возвращает mxc:// URL.
    """
    token, _, hs_url = _ensure_token()
    headers = {'Authorization': 'Bearer ' + token,
               'Content-Type': content_type}
    req = urllib.request.Request(hs_url + '/_matrix/media/v3/upload',
                                 data=data, headers=headers, method='POST')
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        out = json.loads(resp.read() or b'{}')
    except urllib.error.HTTPError as e:
        raw = e.read() or b'{}'
        try:
            out = json.loads(raw)
        except ValueError:
            out = {'error': raw[:200].decode('utf-8', 'replace')}
        raise MatrixError(out.get('error') or f'HTTP {e.code}', e.code)
    except Exception as e:
        raise MatrixError(f'Ошибка загрузки медиа: {e}', 502)
    uri = out.get('content_uri')
    if not uri or not uri.startswith('mxc://'):
        raise MatrixError('Сервер не вернул content_uri', 502)
    return uri


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
            'avatar_url': mxc_to_http(u.get('avatar_url'), 64, 64),
            'avatar_mxc': u.get('avatar_url') or '',
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


def update_user(user_id, action, password=None, displayname=None, admin=None,
                avatar_url=None):
    """Действия: password | deactivate | activate | make_admin | revoke_admin | profile.

    profile — обновление профиля: displayname, admin и/или avatar_url
    (mxc:// или пустая строка — сбросить аватар).
    """
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
    elif action == 'profile':
        body = {}
        if displayname is not None:
            body['displayname'] = displayname.strip()
        if admin is not None:
            body['admin'] = bool(admin)
        if avatar_url is not None:
            au = (avatar_url or '').strip()
            body['avatar_url'] = au or None  # None сбрасывает аватар
        if not body:
            raise MatrixError('Нет данных для обновления профиля', 400)
    else:
        raise MatrixError(f'Неизвестное действие: {action}', 400)
    _request('/_synapse/admin/v2/users/' + _enc_user(user_id), 'PUT', body)
    return True


def list_rooms(limit=100):
    """Комнаты сервера (админ-API v1) + аватарки из state-событий."""
    out, server_name = _request(
        f'/_synapse/admin/v1/rooms?limit={int(limit)}&order_by=name')
    rooms = []
    for r in out.get('rooms') or []:
        room_id = r.get('room_id') or ''
        avatar_mxc = ''
        try:
            av, _ = _request(
                f'/_matrix/client/v3/rooms/{_enc_user(room_id)}'
                f'/state/m.room.avatar')
            avatar_mxc = av.get('url') or ''
        except MatrixError:
            pass
        rooms.append({
            'room_id': room_id,
            'name': r.get('name') or '',
            'topic': r.get('topic') or '',
            'avatar_url': mxc_to_http(avatar_mxc, 96, 96),
            'avatar_mxc': avatar_mxc,
            'members': int(r.get('joined_members') or 0),
            'public': bool(r.get('public')),
            'federatable': bool(r.get('federatable')),
            'creator': r.get('creator') or '',
        })
    rooms.sort(key=lambda x: (-x['members'], x['name'] or x['room_id']))
    return {'rooms': rooms, 'total': out.get('total_rooms', len(rooms)),
            'server_name': server_name}


# ---------------------------------------------------------------------------
# Управление комнатами: создание, инвайт/кик, роли (power levels), удаление
# ---------------------------------------------------------------------------
POWER_LEVELS = {0: 'Участник', 50: 'Модератор', 100: 'Админ'}


def create_room(name, topic='', invite=None, is_direct=False):
    """Создать комнату с понятным названием.

    preset=private_chat (по умолчанию), visibility=private — закрытая комната.
    invite — список user_id, которых сразу пригласить.
    Возвращает room_id.
    """
    name = (name or '').strip()
    if not name:
        raise MatrixError('Название комнаты не может быть пустым', 400)
    body = {
        'name': name,
        'preset': 'private_chat',
        'visibility': 'private',
    }
    if (topic or '').strip():
        body['topic'] = topic.strip()
    invite = [u.strip() for u in (invite or []) if isinstance(u, str)]
    invite = [u for u in invite if u.startswith('@')]
    if invite:
        body['invite'] = invite
    if is_direct:
        body['is_direct'] = True
    out, _ = _request('/_matrix/client/v3/createRoom', 'POST', body)
    room_id = out.get('room_id')
    if not room_id:
        raise MatrixError('Synapse не вернул room_id при создании комнаты', 502)
    return {'room_id': room_id, 'name': name}


def invite_user(room_id, user_id):
    """Пригласить пользователя в комнату."""
    if not user_id or not user_id.startswith('@'):
        raise MatrixError('Некорректный user_id', 400)
    _request(f'/_matrix/client/v3/rooms/{_enc_user(room_id)}/invite',
             'POST', {'user_id': user_id})
    return True


def kick_user(room_id, user_id, reason=''):
    """Исключить пользователя из комнаты (kick)."""
    if not user_id or not user_id.startswith('@'):
        raise MatrixError('Некорректный user_id', 400)
    body = {'user_id': user_id}
    if (reason or '').strip():
        body['reason'] = reason.strip()
    _request(f'/_matrix/client/v3/rooms/{_enc_user(room_id)}/kick',
             'POST', body)
    return True


def get_room_members(room_id):
    """Участники комнаты: joined + invited, с текущими ролями (power levels).

    Если админ не состоит в комнате (client API → 403 room previews
    disabled), берём состав через админ-API v1 rooms/{id}/members — без
    ролей и приглашений (admin_view=True); роли/кик станут доступны после
    join_room().
    """
    if not room_id or not room_id.startswith('!'):
        raise MatrixError('Некорректный room_id', 400)
    try:
        return _room_members_client(room_id)
    except MatrixError as e:
        if e.http_code != 403:
            raise
        return _room_members_admin(room_id)


def _room_members_client(room_id):
    """Состав через client API (нужен membership админа в комнате)."""
    out, _ = _request(
        f'/_matrix/client/v3/rooms/{_enc_user(room_id)}/joined_members')
    joined = out.get('joined') or {}

    # invited (membership=invite) — через /members, invited_members на этой сборке нет
    out2, _ = _request(
        f'/_matrix/client/v3/rooms/{_enc_user(room_id)}'
        f'/members?membership=invite')
    invited_ids = {ev.get('state_key') for ev in out2.get('chunk') or []
                   if ev.get('state_key')}

    # power levels для ролей
    pl_users = {}
    try:
        out3, _ = _request(
            f'/_matrix/client/v3/rooms/{_enc_user(room_id)}'
            f'/state/m.room.power_levels')
        pl_users = out3.get('users') or {}
    except MatrixError:
        pass  # без power levels роли покажем как «Участник» по умолчанию

    members = []
    for uid, info in joined.items():
        level = int(pl_users.get(uid, 0))
        members.append({
            'user_id': uid,
            'displayname': (info or {}).get('display_name') or '',
            'avatar_url': mxc_to_http((info or {}).get('avatar_url'), 48, 48),
            'membership': 'join',
            'power_level': level,
            'role': POWER_LEVELS.get(level, f'Уровень {level}'),
        })
    for uid in sorted(invited_ids):
        members.append({
            'user_id': uid,
            'displayname': '',
            'membership': 'invite',
            'power_level': None,
            'role': 'Приглашён',
        })
    members.sort(key=lambda x: (x['membership'] != 'join',
                                -(x['power_level'] or 0),
                                x['displayname'] or x['user_id']))
    return {'room_id': room_id, 'members': members, 'admin_view': False}


def _room_members_admin(room_id):
    """Состав через админ-API (без membership); роли и invites недоступны."""
    out, _ = _request(
        f'/_synapse/admin/v1/rooms/{_enc_user(room_id)}/members')
    uids = out.get('members') or []
    members = []
    for uid in uids:
        displayname, avatar_mxc = '', ''
        try:
            detail, _ = _request(
                '/_synapse/admin/v2/users/' + _enc_user(uid))
            displayname = detail.get('displayname') or ''
            avatar_mxc = detail.get('avatar_url') or ''
        except MatrixError:
            pass  # профиль не раскрыт — покажем только user_id
        members.append({
            'user_id': uid,
            'displayname': displayname,
            'avatar_url': mxc_to_http(avatar_mxc, 48, 48),
            'membership': 'join',
            'power_level': None,
            'role': '',
        })
    members.sort(key=lambda x: (x['displayname'] or x['user_id']))
    return {'room_id': room_id, 'members': members, 'admin_view': True}


def join_room(room_id):
    """Админ входит в комнату для управления составом и ролями.

    На этой сборке Synapse прямой админ-join (/admin/v1/join) для invite-only
    комнат падает 403: инвайт отправляется от админа, который сам в комнате
    не состоит. Рабочая цепочка (проверено живьём):
    1) клиентский POST /rooms/{id}/join — быстрый путь (админ уже в комнате,
       есть приглашение или комната публичная);
    2) при 403 — admin rooms/{id}/make_room_admin: локальный юзер с правами
       приглашает админа и выдаёт ему power level для управления;
    3) повторный клиентский join принимает приглашение.
    """
    if not room_id or not room_id.startswith('!'):
        raise MatrixError('Некорректный room_id', 400)
    _token, server_name, _ = _ensure_token()
    enc = _enc_user(room_id)
    join_path = f'/_matrix/client/v3/rooms/{enc}/join'
    try:
        _request(join_path, 'POST', {})
        return True
    except MatrixError as e:
        if e.http_code != 403:
            raise
    mra_err = None
    try:
        _request(f'/_synapse/admin/v1/rooms/{enc}/make_room_admin',
                 'POST', {'user_id': _me_user_id(server_name)})
    except MatrixError as e:
        mra_err = e
    try:
        _request(join_path, 'POST', {})
        return True
    except MatrixError as e:
        if mra_err is not None:
            raise MatrixError(f'{mra_err} / {e}', e.http_code or 400)
        raise


def set_power_level(room_id, user_id, level):
    """Изменить роль (power level) пользователя в комнате.

    level: 0 (участник), 50 (модератор), 100 (админ).
    GET текущих power levels → PUT с изменённым users-маппингом.
    """
    if not user_id or not user_id.startswith('@'):
        raise MatrixError('Некорректный user_id', 400)
    level = int(level)
    if level not in POWER_LEVELS:
        raise MatrixError('Уровень должен быть 0 (участник), 50 (модератор) '
                          'или 100 (админ)', 400)
    out, _ = _request(
        f'/_matrix/client/v3/rooms/{_enc_user(room_id)}'
        f'/state/m.room.power_levels')
    body = dict(out)
    users = dict(body.get('users') or {})
    users[user_id] = level
    body['users'] = users
    _request(f'/_matrix/client/v3/rooms/{_enc_user(room_id)}'
             f'/state/m.room.power_levels', 'PUT', body)
    return True


def delete_room(room_id, reason=''):
    """Удалить комнату (админ-API v2 DELETE с телом {room_id, reason}).

    На этой сборке Synapse DELETE /_synapse/admin/v2/rooms/{id} работает
    только с JSON-телом; POST .../delete не распознаётся (404).
    """
    body = {'room_id': room_id}
    if (reason or '').strip():
        body['reason'] = reason.strip()
    _request(f'/_synapse/admin/v2/rooms/{_enc_user(room_id)}', 'DELETE', body)
    return True


def update_room(room_id, name=None, topic=None, avatar_url=None):
    """Изменить название/тему/аватар комнаты через state-события.

    m.room.name, m.room.topic, m.room.avatar — state-события с пустым
    state_key, PUT /rooms/{id}/state/{eventType} (без суффикса txn).
    avatar_url: mxc:// или пустая строка — сбросить аватар.
    """
    if not room_id or not room_id.startswith('!'):
        raise MatrixError('Некорректный room_id', 400)
    if name is None and topic is None and avatar_url is None:
        raise MatrixError('Нет данных для обновления комнаты', 400)
    if name is not None:
        name = name.strip()
        if not name:
            raise MatrixError('Название комнаты не может быть пустым', 400)
        _request(f'/_matrix/client/v3/rooms/{_enc_user(room_id)}'
                 f'/state/m.room.name', 'PUT', {'name': name})
    if topic is not None:
        _request(f'/_matrix/client/v3/rooms/{_enc_user(room_id)}'
                 f'/state/m.room.topic', 'PUT', {'topic': topic.strip()})
    if avatar_url is not None:
        au = (avatar_url or '').strip()
        _request(f'/_matrix/client/v3/rooms/{_enc_user(room_id)}'
                 f'/state/m.room.avatar', 'PUT', {'url': au or None})
    return True


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


def send_broadcast(message, mode='dm', user_ids=None, room_id=None):
    """Рассылка выбранным пользователям.

    mode='dm'  — личное сообщение каждому (комната DM создаётся при
                  необходимости и запоминается в m.direct);
    mode='room' — одно сообщение в выбранный общий рум (room_id).
                  Если room_id не указан — старое поведение: комната
                  «Рассылка VNC Manager» (создаётся/находится, участники
                  приглашаются автоматически).
    user_ids=[]/None — все активные пользователи сервера (только для dm).
    room_id — id комнаты для mode='room' (из списка list_rooms).
    """
    message = (message or '').strip()
    if not message:
        raise MatrixError('Сообщение не может быть пустым', 400)
    if mode not in ('dm', 'room'):
        raise MatrixError(f'Неизвестный режим рассылки: {mode}', 400)

    if mode == 'room' and room_id:
        # Отправка в явно выбранный рум: без автоинвайта, просто сообщение
        room_id = room_id.strip()
        if not room_id.startswith('!'):
            raise MatrixError('Некорректный room_id', 400)
        _send_msg(room_id, message)
        # имя комнаты — из списка, если удалось
        room_name = room_id
        try:
            for r in list_rooms(limit=200)['rooms']:
                if r['room_id'] == room_id:
                    room_name = r['name'] or room_id
                    break
        except MatrixError:
            pass
        return {'mode': 'room', 'room_id': room_id, 'room_name': room_name,
                'created': False, 'total': None, 'invited': 0,
                'sent': 1, 'failed': 0, 'errors': []}

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

    # mode == 'room' без явного room_id — старое поведение «Рассылка VNC Manager»
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
