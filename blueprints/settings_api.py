# -*- coding: utf-8 -*-
"""API пользователей, настроек приложения и имён подсетей."""
import os

from flask import Blueprint, current_app, jsonify, request, session
from werkzeug.security import generate_password_hash

from models import Settings, SubnetName, User, db, utcnow
from services.api_utils import get_json
from services.auth_core import _require_admin

bp = Blueprint('settings_api', __name__)
# --- API управления пользователями (только администратор) ---

def _admin_or_error():
    if _require_admin():
        return None
    return jsonify({'error': 'Недостаточно прав'}), 403


def _serialize_user(u):
    return {
        'id': u.id,
        'username': u.username,
        'role': u.role,
        'created_at': u.created_at.isoformat() if u.created_at else None,
    }


@bp.route('/api/users', methods=['GET'])
def api_users_list():
    err = _admin_or_error()
    if err:
        return err
    users = User.query.order_by(User.username).all()
    return jsonify([_serialize_user(u) for u in users])


@bp.route('/api/users', methods=['POST'])
def api_users_create():
    err = _admin_or_error()
    if err:
        return err
    data = get_json()
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''
    role = (data.get('role') or 'user').strip()

    if len(username) < 3:
        return jsonify({'error': 'Логин должен быть не короче 3 символов'}), 400
    if len(password) < 6:
        return jsonify({'error': 'Пароль должен быть не короче 6 символов'}), 400
    if role not in ('admin', 'user'):
        return jsonify({'error': 'Роль должна быть admin или user'}), 400
    if User.query.filter_by(username=username).first():
        return jsonify({'error': 'Пользователь с таким логином уже существует'}), 400

    user = User(
        username=username,
        password_hash=generate_password_hash(password),
        role=role,
    )
    db.session.add(user)
    db.session.commit()
    return jsonify({'success': True, 'user': _serialize_user(user)})


@bp.route('/api/users/<int:user_id>', methods=['PUT'])
def api_users_update(user_id):
    err = _admin_or_error()
    if err:
        return err
    user = User.query.get(user_id)
    if not user:
        return jsonify({'error': 'Пользователь не найден'}), 404

    data = get_json()
    if 'username' in data:
        new_name = (data['username'] or '').strip()
        if len(new_name) < 3:
            return jsonify({'error': 'Логин должен быть не короче 3 символов'}), 400
        duplicate = User.query.filter(User.username == new_name, User.id != user.id).first()
        if duplicate:
            return jsonify({'error': 'Пользователь с таким логином уже существует'}), 400
        user.username = new_name
        if session.get('user_id') == user.id:
            session['username'] = user.username

    if 'password' in data and data['password']:
        if len(data['password']) < 6:
            return jsonify({'error': 'Пароль должен быть не короче 6 символов'}), 400
        user.password_hash = generate_password_hash(data['password'])

    if 'role' in data:
        new_role = (data['role'] or '').strip()
        if new_role not in ('admin', 'user'):
            return jsonify({'error': 'Роль должна быть admin или user'}), 400
        if new_role != user.role and session.get('user_id') == user.id:
            return jsonify({'error': 'Нельзя менять собственную роль'}), 400
        # Нельзя снять роль админа у последнего администратора.
        if user.role == 'admin' and new_role != 'admin':
            admins = User.query.filter_by(role='admin').count()
            if admins <= 1:
                return jsonify({'error': 'Нельзя убрать роль у последнего администратора'}), 400
        user.role = new_role
        if session.get('user_id') == user.id:
            session['role'] = user.role

    db.session.commit()
    return jsonify({'success': True, 'user': _serialize_user(user)})


@bp.route('/api/users/<int:user_id>', methods=['DELETE'])
def api_users_delete(user_id):
    err = _admin_or_error()
    if err:
        return err
    user = User.query.get(user_id)
    if not user:
        return jsonify({'error': 'Пользователь не найден'}), 404
    if session.get('user_id') == user.id:
        return jsonify({'error': 'Нельзя удалить самого себя'}), 400
    if user.role == 'admin':
        admins = User.query.filter_by(role='admin').count()
        if admins <= 1:
            return jsonify({'error': 'Нельзя удалить последнего администратора'}), 400

    db.session.delete(user)
    db.session.commit()
    return jsonify({'success': True})


@bp.route('/api/settings', methods=['GET'])
def get_settings():
    """Получить все настройки"""
    settings = Settings.query.all()
    result = {}
    for setting in settings:
        # Секреты не отдаём клиенту в открытом виде
        if setting.key in ('rustdesk_api_pass', 'matrix_admin_pass'):
            result[setting.key] = ''
            continue
        # Преобразуем значение в соответствии с типом
        if setting.type == 'boolean':
            result[setting.key] = setting.value.lower() == 'true'
        elif setting.type == 'number':
            try:
                result[setting.key] = float(setting.value)
            except ValueError:
                result[setting.key] = setting.value
        else:
            result[setting.key] = setting.value
    return jsonify(result)

@bp.route('/api/settings', methods=['POST'])
def update_settings():
    """Обновить настройки"""
    try:
        data = get_json()
        
        for key, value in data.items():
            # Пустой пароль не перезаписываем (клиент не получает текущее значение)
            if key in ('rustdesk_api_pass', 'matrix_admin_pass') and not str(value):
                continue
            setting = Settings.query.filter_by(key=key).first()
            
            if setting:
                # Обновляем существующую настройку
                if setting.type == 'boolean':
                    setting.value = 'true' if value else 'false'
                elif setting.type == 'number':
                    setting.value = str(value)
                else:
                    setting.value = str(value)
                setting.updated_at = utcnow()
            else:
                # Создаем новую настройку
                setting_type = 'string'
                setting_value = str(value)
                
                # Определяем тип автоматически
                if isinstance(value, bool):
                    setting_type = 'boolean'
                    setting_value = 'true' if value else 'false'
                elif isinstance(value, (int, float)):
                    setting_type = 'number'
                    setting_value = str(value)
                
                setting = Settings(
                    key=key,
                    value=setting_value,
                    type=setting_type,
                    description=f'Настройка {key}'
                )
                db.session.add(setting)
        
        db.session.commit()
        return jsonify({'success': True})
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

_UPLOAD_EXT_ALLOWED = {
    'favicon': {'ico', 'png', 'jpg', 'jpeg', 'gif', 'svg'},
    'logo': {'png', 'jpg', 'jpeg', 'gif', 'svg', 'webp'},
}

_IMAGE_MAGIC_BYTES = {
    'png': (b'\x89PNG\r\n\x1a\n',),
    'jpg': (b'\xff\xd8\xff',),
    'jpeg': (b'\xff\xd8\xff',),
    'gif': (b'GIF87a', b'GIF89a'),
    'ico': (b'\x00\x00\x01\x00',),
    'webp': (b'RIFF',),
}


def _validate_image_content(file, ext):
    """Проверяет, что содержимое файла соответствует заявленному расширению.

    Для SVG отклоняет файлы с исполняемым содержимым (<script>, обработчики
    событий, javascript:), которые могут привести к XSS при отдаче из своего
    origin.
    """
    file.seek(0)
    head = file.read(64)
    file.seek(0)

    if ext == 'svg':
        body = (head + file.read(16384)).lower()
        file.seek(0)
        if b'<svg' not in body:
            return False
        if any(marker in body for marker in (
            b'<script', b'onload=', b'onerror=', b'onclick=',
            b'javascript:', b'<foreignobject',
        )):
            return False
        return True

    expected = _IMAGE_MAGIC_BYTES.get(ext)
    if not expected:
        return True
    return any(head.startswith(m) for m in expected)


def _save_uploaded_image(file_field, setting_key, base_name):
    """Валидирует и сохраняет загруженное изображение, обновляет настройку."""
    if file_field not in request.files:
        return jsonify({'error': 'Файл не загружен'}), 400

    file = request.files[file_field]
    if file.filename == '':
        return jsonify({'error': 'Файл не выбран'}), 400

    dot = file.filename.rfind('.')
    if dot == -1:
        return jsonify({'error': 'Неверный формат файла'}), 400
    ext = file.filename[dot + 1:].lower()
    if ext not in _UPLOAD_EXT_ALLOWED[base_name]:
        return jsonify({'error': 'Неверный формат файла'}), 400

    if not _validate_image_content(file, ext):
        return jsonify({'error': 'Файл повреждён или содержит недопустимое содержимое'}), 400

    static_dir = os.path.join(current_app.static_folder, 'uploads')
    os.makedirs(static_dir, exist_ok=True)

    filename = f"{base_name}.{ext}"
    file_path = os.path.join(static_dir, filename)
    file.save(file_path)

    setting = Settings.query.filter_by(key=setting_key).first()
    if setting:
        setting.value = f"uploads/{filename}"
        setting.updated_at = utcnow()
    else:
        setting = Settings(
            key=setting_key,
            value=f"uploads/{filename}",
            type='string',
            description=f'Путь к файлу {base_name}'
        )
        db.session.add(setting)

    db.session.commit()
    return jsonify({'success': True, 'path': f"uploads/{filename}"})


@bp.route('/api/settings/upload_favicon', methods=['POST'])
def upload_favicon():
    """Загрузить favicon"""
    try:
        return _save_uploaded_image('favicon', 'favicon_path', 'favicon')
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


@bp.route('/api/settings/upload_logo', methods=['POST'])
def upload_logo():
    """Загрузить логотип"""
    try:
        return _save_uploaded_image('logo', 'logo_path', 'logo')
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

# API для названий подсетей
@bp.route('/api/subnet_names', methods=['GET'])
def get_subnet_names():
    try:
        subnet_names = SubnetName.query.all()
        return jsonify([{'subnet': sn.subnet, 'name': sn.name} for sn in subnet_names])
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@bp.route('/api/subnet_names', methods=['POST'])
def create_or_update_subnet_name():
    try:
        data = request.get_json()
        subnet = data.get('subnet')
        name = data.get('name')
        
        if not subnet:
            return jsonify({'error': 'Подсеть обязательна'}), 400
        
        # Ищем существующую запись
        subnet_name = SubnetName.query.filter_by(subnet=subnet).first()
        
        if subnet_name:
            if name:
                subnet_name.name = name
            else:
                # Если имя пустое, удаляем запись
                db.session.delete(subnet_name)
        else:
            if name:
                # Создаем новую запись
                subnet_name = SubnetName(subnet=subnet, name=name)
                db.session.add(subnet_name)
        
        db.session.commit()
        return jsonify({'success': True})
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

@bp.route('/api/subnet_names/<subnet>', methods=['DELETE'])
def delete_subnet_name(subnet):
    try:
        subnet_name = SubnetName.query.filter_by(subnet=subnet).first()
        if subnet_name:
            db.session.delete(subnet_name)
            db.session.commit()
            return jsonify({'success': True})
        else:
            return jsonify({'error': 'Название подсети не найдено'}), 404
            
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

