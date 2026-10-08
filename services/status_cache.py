# -*- coding: utf-8 -*-
"""Кэш статусов устройств: TCP/HTTP/SNMP-пробы и параллельные проверки."""
import concurrent.futures
import os
import platform
import socket
import subprocess
import threading
import time

STATUS_CACHE_TTL = int(os.environ.get('STATUS_CACHE_TTL', '30'))


_status_cache = {}
_status_cache_lock = threading.Lock()


def get_server_status_cached(ip, port):
    key = (ip, int(port))
    now = time.time()
    with _status_cache_lock:
        cached = _status_cache.get(key)
        if cached and cached[1] > now:
            return cached[0]
    online = check_server_status(ip, port)
    with _status_cache_lock:
        _status_cache[key] = (online, now + STATUS_CACHE_TTL)
    return online


def get_printer_status_cached(ip):
    key = ('printer', ip)
    now = time.time()
    with _status_cache_lock:
        cached = _status_cache.get(key)
        if cached and cached[1] > now:
            return cached[0]
    online = check_printer_status(ip)
    with _status_cache_lock:
        _status_cache[key] = (online, now + STATUS_CACHE_TTL)
    return online


def pick_reachable_endpoint(ip, port, alt_endpoints=None):
    """Возвращает первый доступный адрес из кэша статусов, иначе основной.

    alt_endpoints — список [(ip, port)]. Проверка идёт по кэшу (TTL-обёртки),
    поэтому стоимость — не более одной сетевой пробы на адрес.
    """
    primary = (str(ip), int(port or 5900))
    candidates = [primary]
    for host, p in (alt_endpoints or []):
        endpoint = (str(host), int(p or 5900))
        if endpoint != primary and endpoint not in candidates:
            candidates.append(endpoint)
    for host, p in candidates:
        if get_server_status_cached(host, p):
            return host, p
    return primary


def _fetch_statuses_parallel(items, is_printer=False, is_web=False):
    """Параллельно проверяет статусы устройств, возвращает {id: bool}.

    Отдельные ошибки проб не роняют общий запрос — такие устройства
    помечаются как offline. У серверов проверяются все адреса
    (основной + alts): устройство online, если доступен хотя бы один.
    """
    result = {}
    if not items:
        return result

    def probe(item):
        if is_printer:
            return item.id, get_printer_status_cached(item.ip)
        if is_web:
            return item.id, get_web_device_status_cached(item.ip, item.port)
        endpoints = item.all_endpoints()
        return item.id, any(
            get_server_status_cached(host, p) for host, p in endpoints
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(32, len(items))) as executor:
        future_to_item = {executor.submit(probe, item): item for item in items}
        for future in concurrent.futures.as_completed(future_to_item):
            try:
                dev_id, online = future.result()
                result[dev_id] = online
            except Exception:
                dev_id = future_to_item[future].id
                result[dev_id] = False
    return result


def _tcp_probe_printer(host: str) -> bool:
    for p in (9100, 631, 515, 80, 443):
        try:
            with socket.create_connection((host, p), timeout=1):
                return True
        except OSError:
            continue
    return False


def check_server_status(ip, port=5900):
    """Проверка статуса сервера VNC"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        result = sock.connect_ex((ip, port))
        sock.close()
        return result == 0
    except OSError:
        return False


def check_printer_status(ip):
    """Проверка статуса принтера (ping + TCP fallback)"""
    try:
        if platform.system() == 'Windows':
            cmd = ['ping', '-n', '1', '-w', '1000', ip]
        else:
            cmd = ['ping', '-c', '1', '-W', '1', ip]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
        if result.returncode == 0:
            return True
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return _tcp_probe_printer(ip)

def check_web_device_status(ip, port):
    """Проверка статуса веб-устройства (камеры/роутеры): ping + TCP fallback"""
    try:
        if platform.system() == 'Windows':
            cmd = ['ping', '-n', '1', '-w', '1000', ip]
        else:
            cmd = ['ping', '-c', '1', '-W', '1', ip]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
        if result.returncode == 0:
            return True
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1.5)
        r = sock.connect_ex((ip, int(port or 80)))
        sock.close()
        return r == 0
    except OSError:
        return False

def get_web_device_status_cached(ip, port):
    key = ('web', ip, int(port or 80))
    now = time.time()
    with _status_cache_lock:
        cached = _status_cache.get(key)
        if cached and cached[1] > now:
            return cached[0]
    online = check_web_device_status(ip, port)
    with _status_cache_lock:
        _status_cache[key] = (online, now + STATUS_CACHE_TTL)
    return online

