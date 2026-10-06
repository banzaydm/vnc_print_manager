from flask import Flask, request, jsonify, redirect, url_for, abort
import os
import secrets
import hmac
from urllib.parse import urlsplit
import platform
from werkzeug.security import generate_password_hash
from models import db, Group, Server, Printer, Settings, User
from services.auth_core import AUTH_ENABLED, _is_authenticated
from blueprints.auth import bp as auth_bp
from blueprints.settings_api import bp as settings_api_bp
from blueprints.data import bp as data_bp
from blueprints.devices import bp as devices_bp
from blueprints.streams import bp as streams_bp
from blueprints.rustdesk import bp as rustdesk_bp
from blueprints.scan import bp as scan_bp
from blueprints.vnc import bp as vnc_bp

from services.novnc_core import NOVNC_PROXY_PORT, NOVNC_WS_PATH, _NOVNC_TOKEN_FILE

# Переэкспорт: run.py делает `from app import NOVNC_PROXY_PORT, _NOVNC_TOKEN_FILE`,
# поэтому имена обязаны оставаться атрибутами модуля app.
__all__ = ['NOVNC_PROXY_PORT', '_NOVNC_TOKEN_FILE']
from sqlalchemy import text

app = Flask(__name__, static_folder='.', static_url_path='')
os.makedirs(app.instance_path, exist_ok=True)
_db_path = os.path.join(app.instance_path, 'vnc_manager.db')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + _db_path.replace('\\', '/')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)

API_KEY = os.environ.get('API_KEY', '').strip()
BACKUPS_DIR = os.path.join(app.instance_path, 'backups')
os.makedirs(BACKUPS_DIR, exist_ok=True)

app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', '').strip()
if not app.config['SECRET_KEY']:
    # Постоянный случайный ключ, чтобы сессии переживали перезапуск.
    _secret_file = os.path.join(app.instance_path, 'secret_key')
    try:
        with open(_secret_file, 'r', encoding='utf-8') as f:
            app.config['SECRET_KEY'] = f.read().strip()
    except OSError:
        pass
    if not app.config['SECRET_KEY']:
        app.config['SECRET_KEY'] = secrets.token_hex(32)
        try:
            with open(_secret_file, 'w', encoding='utf-8') as f:
                f.write(app.config['SECRET_KEY'])
        except OSError:
            pass


# Эндпоинты, доступные без API-ключа.
# По умолчанию защищены ВСЕ /api/* — новый эндпоинт нельзя «забыть» в списке.
_PUBLIC_API_EXACT = frozenset({
    '/api/config',
    '/api/settings',
})



def _is_protected_api(path: str) -> bool:
    if path in _PUBLIC_API_EXACT:
        return False
    return path.startswith('/api/')


def _extract_api_key():
    auth = request.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        return auth[7:].strip()
    return request.headers.get('X-API-Key', '').strip()


def _require_api_key():
    if not API_KEY:
        return None
    provided = _extract_api_key()
    if not provided or not hmac.compare_digest(provided, API_KEY):
        return jsonify({'error': 'Требуется API ключ'}), 401
    return None


@app.after_request
def add_api_cors_headers(response):
    if request.path.startswith('/api/'):
        response.headers.setdefault('Access-Control-Allow-Origin', '*')
        response.headers.setdefault('Access-Control-Allow-Headers', 'Content-Type, Authorization, X-API-Key')
        response.headers.setdefault('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS')
    return response


@app.route('/api/<path:any_path>', methods=['OPTIONS'])
def api_options(any_path):
    return ('', 204)


@app.route('/api/config', methods=['GET'])
def api_config():
    return jsonify({
        'auth_required': bool(API_KEY),
        'novnc_proxy_port': NOVNC_PROXY_PORT,
        'novnc_ws_path': NOVNC_WS_PATH,
    })


@app.before_request
def _api_auth_and_csrf():
    if not request.path.startswith('/api/'):
        return None
    if request.method == 'OPTIONS':
        return None

    # Защита от CSRF: для state-changing запросов Origin (если прислан)
    # должен совпадать с Host запроса. Сравниваем hostname без порта,
    # чтобы не ломать работу за reverse proxy. Запросы без Origin
    # (небраузерные клиенты) не блокируются.
    if request.method in ('POST', 'PUT', 'DELETE', 'PATCH'):
        origin = request.headers.get('Origin')
        if origin:
            try:
                origin_hostname = urlsplit(origin).hostname
                request_hostname = urlsplit('http://' + request.host).hostname
            except ValueError:
                origin_hostname = request_hostname = None
            if origin_hostname and origin_hostname != request_hostname:
                return jsonify({'error': 'Cross-origin request forbidden'}), 403

    if _is_protected_api(request.path):
        if _is_authenticated():
            return None  # Авторизованная сессия имеет приоритет над API-ключом
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
    return None


# Гейт авторизации: защищает страницы приложения и API от анонимного доступа.
# Статические файлы (style.css, uploads/, login.html и т.п.) остаются публичными.
_HTML_PAGE_PATHS = frozenset({'/', '/index.html'})
_PUBLIC_AUTH_PATHS = frozenset({'/login', '/logout', '/api/config'})


@app.before_request
def _auth_gate():
    if not AUTH_ENABLED:
        return None
    if request.method == 'OPTIONS':
        return None
    path = request.path
    if path in _PUBLIC_AUTH_PATHS:
        return None

    is_api = path.startswith('/api/')
    is_page = path in _HTML_PAGE_PATHS or path.startswith('/novnc/') or path.startswith('/rustdesk/') or path.startswith('/camera_view/')
    if not is_api and not is_page:
        return None  # статика — публична

    if _is_authenticated():
        return None

    if is_api:
        # Внешние клиенты могут работать по API-ключу даже без сессии.
        if API_KEY and _extract_api_key() and hmac.compare_digest(_extract_api_key(), API_KEY):
            return None
        return jsonify({'error': 'Требуется авторизация'}), 401

    return redirect(url_for('auth.login', next=path))


# --- Запрет отдачи чувствительных файлов статикой ---
# Приложение раздаёт статику из корня проекта (static_folder='.'), а auth-гейт
# выше пропускает всё, что не /api/ и не страница приложения. Без этой
# проверки по HTTP анонимно доступны исходники (/app.py), база данных
# (/instance/vnc_manager.db), секрет сессий (/instance/secret_key),
# токены noVNC и содержимое репозитория (/.git/config).
_STATIC_DENY_PREFIXES = ('/instance/', '/.git/', '/venv/', '/__pycache__/', '/backups/')
_STATIC_DENY_EXTS = frozenset({
    '.py', '.pyc', '.pyo', '.db', '.sqlite', '.sqlite3', '.md', '.yml', '.yaml',
    '.sh', '.ps1', '.txt', '.log', '.bak', '.old', '.orig', '.env', '.cfg',
    '.ini', '.toml', '.csv', '.sql',
})
_STATIC_DENY_NAMES = frozenset({'dockerfile', '.dockerignore', '.gitignore'})


@app.before_request
def _static_guard():
    """Ограничивает раздачу статики: только rustdesk_web/, uploads/, hls.min.js,
    style.css и страницы. Всё чувствительное — 404, как будто его нет."""
    if request.endpoint != 'static':
        return None
    path = request.path
    if path.startswith(_STATIC_DENY_PREFIXES):
        abort(404)
    name = os.path.basename(path).lower()
    if name in _STATIC_DENY_NAMES:
        abort(404)
    if os.path.splitext(name)[1] in _STATIC_DENY_EXTS:
        abort(404)
    return None


@app.errorhandler(404)
def handle_404(e):
    if request.path.startswith('/api/'):
        return jsonify({'error': 'Not Found', 'path': request.path}), 404
    return e


@app.errorhandler(405)
def handle_405(e):
    if request.path.startswith('/api/'):
        return jsonify({'error': 'Method Not Allowed', 'path': request.path}), 405
    return e


def _ensure_column(table_name: str, column: str, alter_sql: str):
    """Добавляет колонку в существующую таблицу SQLite, если её ещё нет."""
    try:
        cols = [r[1] for r in db.session.execute(text(f"PRAGMA table_info({table_name})"))]
        if column not in cols:
            db.session.execute(text(alter_sql))
            db.session.commit()
    except Exception:
        db.session.rollback()


def init_db():
    """Инициализация базы данных и лёгкие миграции для существующих БД"""
    with app.app_context():
        db.create_all()

        # Первый администратор из переменных окружения (AUTH_ENABLED=1).
        if AUTH_ENABLED and User.query.count() == 0:
            admin_user = (os.environ.get('ADMIN_USERNAME') or '').strip()
            admin_pass = os.environ.get('ADMIN_PASSWORD') or ''
            if admin_user and admin_pass:
                db.session.add(User(
                    username=admin_user,
                    password_hash=generate_password_hash(admin_pass),
                    role='admin',
                ))
                db.session.commit()
                print(f"Создан администратор '{admin_user}' из переменных окружения")

        # Миграции: добавляем колонки is_favorite в существующие таблицы
        _ensure_column('printer', 'is_favorite', "ALTER TABLE printer ADD COLUMN is_favorite BOOLEAN DEFAULT 0")
        _ensure_column('server', 'is_favorite', "ALTER TABLE server ADD COLUMN is_favorite BOOLEAN DEFAULT 0")
        _ensure_column('server', 'rustdesk_id', "ALTER TABLE server ADD COLUMN rustdesk_id VARCHAR(100) DEFAULT ''")

        # Настройки по умолчанию (только если таблица пуста)
        if Settings.query.count() == 0:
            default_settings = [
                Settings(key='theme', value='light', type='string', description='Цветовая тема (light/dark)'),
                Settings(key='favicon_path', value='', type='string', description='Путь к файлу favicon'),
                Settings(key='logo_path', value='', type='string', description='Путь к файлу логотипа'),
                Settings(key='app_title', value='VNC Manager', type='string', description='Заголовок приложения'),
                Settings(key='primary_color', value='#4a6cf7', type='string', description='Основной цвет темы'),
                Settings(key='custom_css', value='', type='string', description='Пользовательские CSS стили'),
                Settings(key='rustdesk_server', value='', type='string', description='Адрес RustDesk-сервера (hbbs/hbbr)'),
                Settings(key='rustdesk_key', value='', type='string', description='Публичный ключ RustDesk-сервера'),
                Settings(key='rustdesk_api_url', value='', type='string', description='Адрес панели RustDesk API (lejianwen/rustdesk-api)'),
                Settings(key='rustdesk_api_user', value='', type='string', description='Логин панели RustDesk API'),
                Settings(key='rustdesk_api_pass', value='', type='string', description='Пароль панели RustDesk API'),
            ]
            for setting in default_settings:
                db.session.add(setting)
            db.session.commit()
        
        seed_demo = os.environ.get('SEED_DEMO_DATA', '0').strip().lower() in {'1', 'true', 'yes', 'on'}
        # Создаем тестовые группы если их нет (только при явном SEED_DEMO_DATA=1)
        if seed_demo and Group.query.count() == 0:
            groups = [
                Group(name="Серверы отдела", color="#3498db", parent_id=None),
                Group(name="Принтеры", color="#e74c3c", parent_id=None),
                Group(name="Производство", color="#2ecc71", parent_id=None),
                Group(name="Офис", color="#f39c12", parent_id=None),
                Group(name="Склад", color="#9b59b6", parent_id=None),
            ]
            for group in groups:
                db.session.add(group)
            db.session.commit()
            
            # Получаем ID созданных групп
            group_map = {g.name: g.id for g in Group.query.all()}
            
            # Тестовые серверы
            servers = [
                Server(name="Сервер 1", ip="192.168.1.100", port=5900, 
                      group_id=group_map["Серверы отдела"], is_favorite=False, 
                      comment="Основной сервер"),
                Server(name="Сервер 2", ip="192.168.1.101", port=5901, 
                      group_id=group_map["Серверы отдела"], is_favorite=True, 
                      comment="Резервный сервер"),
                Server(name="Производство 1", ip="192.168.1.200", port=5900, 
                      group_id=group_map["Производство"], comment=""),
            ]
            
            for server in servers:
                db.session.add(server)
            
            # Тестовые принтеры
            printers = [
                Printer(name="Принтер HP", ip="192.168.1.50", 
                       group_id=group_map["Принтеры"], 
                       web_interface="http://192.168.1.50", 
                       comment="Основной принтер"),
                Printer(name="Копир Canon", ip="192.168.1.51", 
                       group_id=group_map["Принтеры"],
                       web_interface="http://192.168.1.51"),
                Printer(name="Складской принтер", ip="192.168.1.52", 
                       group_id=group_map["Склад"]),
            ]
            
            for printer in printers:
                db.session.add(printer)
            
            db.session.commit()
            print("База данных инициализирована с тестовыми данными")

@app.route('/')
def index():
    return app.send_static_file('index.html')


# Авторизация вынесена в blueprint (см. blueprints/auth.py)
app.register_blueprint(auth_bp)
app.register_blueprint(settings_api_bp)
app.register_blueprint(data_bp)
app.register_blueprint(devices_bp)
app.register_blueprint(streams_bp)
app.register_blueprint(rustdesk_bp)
app.register_blueprint(scan_bp)
app.register_blueprint(vnc_bp)


# Инициализация при запуске
_INIT_DB_ON_START = os.environ.get("INIT_DB_ON_START", "1").strip().lower() not in {"0", "false", "no", "off"}
if _INIT_DB_ON_START:
    with app.app_context():
        init_db()

if __name__ == '__main__':
    print("=" * 50)
    print("VNC & Printer Manager запущен!")
    print("=" * 50)
    print(f"Платформа: {platform.system()}")
    print("Откройте в браузере: http://localhost:5000")
    print("Доступно по сети: http://<ваш_ip>:5000")
    print("=" * 50)

    debug = os.environ.get('FLASK_DEBUG', '0').strip().lower() in {'1', 'true', 'yes', 'on'}
    app.run(debug=debug, host='0.0.0.0', port=5000, use_reloader=False)
