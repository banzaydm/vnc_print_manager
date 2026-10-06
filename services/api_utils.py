"""Общие помощники API: JSON-тело запроса и нормализация портов."""
from flask import request


def get_json():
    return request.get_json(silent=True) or {}


def _normalize_port(value, default=5900):
    """Возвращает валидный порт (1-65535) или None."""
    try:
        port = int(value)
    except (TypeError, ValueError):
        return None
    if 1 <= port <= 65535:
        return port
    return None
