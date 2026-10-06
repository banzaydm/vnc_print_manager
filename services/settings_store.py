"""Чтение настроек приложения из таблицы Settings."""
from models import Settings


def _get_setting(key, default=None):
    setting = Settings.query.filter_by(key=key).first()
    if setting and setting.value:
        return setting.value
    return default
