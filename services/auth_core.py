"""Сессионная авторизация: флаг AUTH_ENABLED и проверки текущего пользователя."""
import os

from flask import session

from models import User

AUTH_ENABLED = os.environ.get('AUTH_ENABLED', '1').strip().lower() not in {'0', 'false', 'no', 'off'}


def _current_user():
    """Возвращает текущего авторизованного пользователя или None."""
    if not AUTH_ENABLED:
        return None
    user_id = session.get('user_id')
    if not user_id:
        return None
    return User.query.get(user_id)


def _is_authenticated():
    if not AUTH_ENABLED:
        return True
    return session.get('user_id') is not None


def _require_admin():
    """True, если текущий запрос выполняется с правами администратора."""
    if not AUTH_ENABLED:
        return True
    user = _current_user()
    return user is not None and user.role == 'admin'
