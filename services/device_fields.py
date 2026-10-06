"""Общие поля устройств, которые используют и CRUD-роуты, и импорт данных."""


def _apply_password(device, data):
    """Сохраняет пароль, если прислано реальное значение (не маска и не пусто)."""
    pw = data.get('password')
    if isinstance(pw, str) and pw and pw != '******':
        device.password = pw
    elif isinstance(pw, str) and pw == '' and data.get('clear_password'):
        device.password = ''
