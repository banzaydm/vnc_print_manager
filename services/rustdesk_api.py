"""Клиент панели RustDesk API (lejianwen/rustdesk-api).

Логин, получение списка пиров и кэши: токена (пока жив) и списка пиров
(30 секунд — чтобы несколько вкладок браузера не дёргали панель).
"""
import threading
import time

import requests

# ---------------------------------------------------------------------------
# RustDesk API (lejianwen/rustdesk-api): список устройств и их онлайн-статус
# ---------------------------------------------------------------------------
_rd_token_cache = {}
_rd_token_lock = threading.Lock()
_RD_ONLINE_WINDOW = 300  # секунд: считаем устройство онлайн


def _rustdesk_login(api_url, username, password):
    """Возвращает (token, error)."""
    try:
        r = requests.post(
            api_url.rstrip('/') + '/api/login',
            json={'username': username, 'password': password},
            timeout=8,
        )
    except requests.RequestException as e:
        return None, 'Не удалось подключиться к RustDesk API: %s' % e
    if r.status_code != 200:
        return None, 'Ошибка авторизации RustDesk API (HTTP %s): %s' % (r.status_code, r.text[:200])
    try:
        data = r.json()
    except ValueError:
        return None, 'Некорректный ответ RustDesk API при авторизации'
    token = data.get('access_token')
    if not token:
        return None, 'RustDesk API не вернул токен доступа'
    return token, None


def _rd_fetch_peers(api_url, username, password):
    """Список пиров из панели RustDesk API. Возвращает (peers, error, code)."""
    def get_peer_list(token):
        return requests.get(
            api_url.rstrip('/') + '/api/admin/peer/list',
            params={'page': 1, 'page_size': 500},
            headers={'api-token': token},
            timeout=10,
        )

    token = None
    with _rd_token_lock:
        cached = _rd_token_cache.get(api_url)
        if cached:
            token = cached
    if not token:
        with _rd_token_lock:
            token, err = _rustdesk_login(api_url, username, password)
        if err:
            return None, err, 502
        with _rd_token_lock:
            _rd_token_cache[api_url] = token

    try:
        r = get_peer_list(token)
    except requests.RequestException as e:
        with _rd_token_lock:
            _rd_token_cache.pop(api_url, None)
        return None, 'Ошибка запроса к RustDesk API: %s' % e, 502

    if r.status_code == 403:
        # Токен протух — перелогиниваемся и пробуем ещё раз
        with _rd_token_lock:
            token, err = _rustdesk_login(api_url, username, password)
        if err:
            return None, err, 502
        with _rd_token_lock:
            _rd_token_cache[api_url] = token
        try:
            r = get_peer_list(token)
        except requests.RequestException as e:
            return None, 'Ошибка запроса к RustDesk API: %s' % e, 502

    if r.status_code != 200:
        return None, 'RustDesk API вернул HTTP %s' % r.status_code, 502
    try:
        data = r.json()
    except ValueError:
        return None, 'Некорректный ответ RustDesk API', 502

    payload = data.get('data') or {}
    return payload.get('list') or [], None, None


_rd_peer_cache = {}  # api_url -> (timestamp, peers)


def _rd_fetch_peers_cached(api_url, username, password, ttl=30):
    """Кэш списка пиров на ttl секунд.

    Несколько вкладок браузера опрашивают статусы RustDesk одновременно,
    а каждый походный запрос к панели — это login + peer/list. Без кэша
    панель получала бы нагрузку, кратную числу открытых вкладок.
    """
    now = time.time()
    with _rd_token_lock:
        entry = _rd_peer_cache.get(api_url)
    if entry and (now - entry[0]) < ttl:
        return entry[1], None, None
    peers, err, code = _rd_fetch_peers(api_url, username, password)
    if err is None:
        with _rd_token_lock:
            _rd_peer_cache[api_url] = (time.time(), peers)
    return peers, err, code
