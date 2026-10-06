"""Авторизация: страница входа, выход и сведения о текущем пользователе."""
import os

from flask import (
    Blueprint, current_app, jsonify, redirect, render_template_string, request,
    session, url_for,
)
from werkzeug.security import generate_password_hash, check_password_hash

from models import User, db
from services.auth_core import AUTH_ENABLED, _current_user, _is_authenticated
from services.settings_store import _get_setting

bp = Blueprint('auth', __name__)


def _render_login(**ctx):
    try:
        with open(os.path.join(current_app.static_folder, 'login.html'), 'r', encoding='utf-8') as f:
            tpl = f.read()
    except OSError:
        tpl = '<h1>Страница входа не найдена (login.html)</h1>'
    return render_template_string(tpl, **ctx)


def _safe_next_url():
    """Безопасный next после входа: только локальные пути."""
    next_url = request.args.get('next') or request.form.get('next') or ''
    if next_url and next_url.startswith('/') and not next_url.startswith('//'):
        return next_url
    return url_for('index')


@bp.route('/login', methods=['GET', 'POST'])
def login():
    if not AUTH_ENABLED:
        return redirect(url_for('index'))
    if _is_authenticated():
        return redirect(_safe_next_url())

    error = None
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''

        # Первичная настройка: если пользователей ещё нет — создаём первого администратора.
        if User.query.count() == 0 and request.form.get('setup') == '1':
            if len(username) < 3:
                error = 'Логин должен быть не короче 3 символов'
            elif len(password) < 6:
                error = 'Пароль должен быть не короче 6 символов'
            else:
                try:
                    admin = User(
                        username=username,
                        password_hash=generate_password_hash(password),
                        role='admin',
                    )
                    db.session.add(admin)
                    db.session.commit()
                    session.clear()
                    session['user_id'] = admin.id
                    session['username'] = admin.username
                    session['role'] = admin.role
                    return redirect(_safe_next_url())
                except Exception:
                    db.session.rollback()
                    error = 'Не удалось создать администратора (возможно, такой логин уже занят)'
        else:
            user = User.query.filter_by(username=username).first()
            if user and check_password_hash(user.password_hash, password):
                session.clear()
                session['user_id'] = user.id
                session['username'] = user.username
                session['role'] = user.role
                return redirect(_safe_next_url())
            error = 'Неверный логин или пароль'

    need_setup = User.query.count() == 0
    return _render_login(
        error=error,
        need_setup=need_setup,
        app_title=_get_setting('app_title', 'VNC Manager'),
        favicon_path=_get_setting('favicon_path', ''),
        logo_path=_get_setting('logo_path', ''),
    )


@bp.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('auth.login'))


@bp.route('/api/me', methods=['GET'])
def api_me():
    """Текущий пользователь. Используется фронтендом для ролей и выхода."""
    if not AUTH_ENABLED:
        return jsonify({
            'authenticated': True,
            'auth_enabled': False,
            'username': None,
            'role': 'admin',
        })
    user = _current_user()
    if not user:
        return jsonify({'authenticated': False, 'auth_enabled': True}), 401
    return jsonify({
        'authenticated': True,
        'auth_enabled': True,
        'username': user.username,
        'role': user.role,
    })
