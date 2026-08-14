from flask import Flask, request, jsonify, session, redirect, url_for, render_template_string, send_file, abort
import subprocess
import os
import socket
import secrets
import threading
import time
import json
import re
import hmac
import tempfile
from datetime import datetime
from urllib.parse import urlsplit, quote
import platform
import ipaddress
import concurrent.futures
import requests
from markupsafe import escape
from werkzeug.security import generate_password_hash, check_password_hash
from models import db, Group, Server, Printer, Camera, Router, Settings, SubnetName, User, utcnow
from sqlalchemy import text

app = Flask(__name__, static_folder='.', static_url_path='')
os.makedirs(app.instance_path, exist_ok=True)
_db_path = os.path.join(app.instance_path, 'vnc_manager.db')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + _db_path.replace('\\', '/')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)

API_KEY = os.environ.get('API_KEY', '').strip()
STATUS_CACHE_TTL = int(os.environ.get('STATUS_CACHE_TTL', '30'))
BACKUPS_DIR = os.path.join(app.instance_path, 'backups')
os.makedirs(BACKUPS_DIR, exist_ok=True)

# --- Авторизация через сессии ---
AUTH_ENABLED = os.environ.get('AUTH_ENABLED', '1').strip().lower() not in {'0', 'false', 'no', 'off'}
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

NOVNC_PROXY_PORT = int(os.environ.get("NOVNC_PROXY_PORT", "6080"))
NOVNC_TOKEN_TTL_SECONDS = int(os.environ.get("NOVNC_TOKEN_TTL_SECONDS", "600"))
NOVNC_WS_PATH = os.environ.get("NOVNC_WS_PATH", "").strip()
NOVNC_CDN_VERSION = os.environ.get("NOVNC_CDN_VERSION", "1.5.0")
_NOVNC_TOKEN_FILE = os.path.join(app.instance_path, "novnc_tokens.txt")

# Эндпоинты, доступные без API-ключа.
# По умолчанию защищены ВСЕ /api/* — новый эндпоинт нельзя «забыть» в списке.
_PUBLIC_API_EXACT = frozenset({
    '/api/config',
    '/api/settings',
})

_status_cache = {}
_status_cache_lock = threading.Lock()
BACKUP_FILENAME_RE = re.compile(r'^backup_\d{8}_\d{6}\.json$')


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


def _safe_backup_path(filename: str):
    if not filename or not BACKUP_FILENAME_RE.fullmatch(filename):
        return None
    path = os.path.join(BACKUPS_DIR, filename)
    if os.path.realpath(path).startswith(os.path.realpath(BACKUPS_DIR)):
        return path
    return None


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


def _fetch_statuses_parallel(items, is_printer=False, is_web=False):
    """Параллельно проверяет статусы устройств, возвращает {id: bool}.

    Отдельные ошибки проб не роняют общий запрос — такие устройства
    помечаются как offline.
    """
    result = {}
    if not items:
        return result

    def probe(item):
        if is_printer:
            return item.id, get_printer_status_cached(item.ip)
        if is_web:
            return item.id, get_web_device_status_cached(item.ip, item.port)
        return item.id, get_server_status_cached(item.ip, item.port)

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

    return redirect(url_for('login', next=path))


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

# --- noVNC / websockify ---
if not os.path.exists(_NOVNC_TOKEN_FILE):
    with open(_NOVNC_TOKEN_FILE, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n")

_novnc_lock = threading.Lock()
_novnc_tokens = {}  # token -> (host, port, expires_at_epoch)


def _prune_novnc_tokens_locked(now: float) -> None:
    expired = [t for t, (_, __, exp) in _novnc_tokens.items() if exp <= now]
    for t in expired:
        _novnc_tokens.pop(t, None)


def _write_novnc_token_file_locked() -> None:
    # TokenFile expects lines: token: host:port
    lines = []
    for token, (host, port, exp) in _novnc_tokens.items():
        if exp > time.time():
            lines.append(f"{token}: {host}:{int(port)}")
    tmp = _NOVNC_TOKEN_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))
        f.write("\n")
    os.replace(tmp, _NOVNC_TOKEN_FILE)


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

def find_vnc_client():
    """Поиск VNC клиента на текущей платформе"""
    system = platform.system()
    
    if system == 'Darwin':  # macOS
        return find_vnc_client_mac()
    elif system == 'Windows':
        return find_vnc_client_windows()
    elif system == 'Linux':
        return find_vnc_client_linux()
    else:
        return None, None

def find_vnc_client_mac():
    """Поиск VNC клиента на macOS"""
    clients = [
        ("RealVNC", "/Applications/RealVNC Viewer.app/Contents/MacOS/vncviewer"),
        ("Chicken VNC", "/Applications/Chicken.app/Contents/MacOS/Chicken"),
        ("VNC Viewer", "/Applications/VNC Viewer.app/Contents/MacOS/vncviewer"),
        ("Screen Sharing", "/System/Library/CoreServices/Applications/Screen Sharing.app/Contents/MacOS/Screen Sharing"),
    ]
    
    for client_name, path in clients:
        if os.path.exists(path):
            return path, client_name
    
    return None, None

def find_vnc_client_windows():
    """Поиск VNC клиента на Windows"""
    program_paths = [
        "C:\\Program Files\\RealVNC\\VNC Viewer\\vncviewer.exe",
        "C:\\Program Files (x86)\\RealVNC\\VNC Viewer\\vncviewer.exe",
        "C:\\Program Files\\TightVNC\\tvnviewer.exe",
        "C:\\Program Files (x86)\\TightVNC\\tvnviewer.exe",
    ]
    
    for path in program_paths:
        if os.path.exists(path):
            client_name = os.path.basename(os.path.dirname(path))
            return path, client_name
    
    return None, None

def find_vnc_client_linux():
    """Поиск VNC клиента на Linux"""
    common_paths = [
        "/usr/bin/vncviewer",
        "/usr/local/bin/vncviewer",
        "/usr/bin/xtightvncviewer",
        "/usr/bin/xtigervncviewer",
        "/usr/bin/vinagre",
        "/usr/bin/remmina",
    ]
    
    for path in common_paths:
        if os.path.exists(path):
            client_name = os.path.basename(path)
            return path, client_name
    
    return None, None

@app.route('/')
def index():
    return app.send_static_file('index.html')


# --- Авторизация (страница входа) ---

_LOGIN_TEMPLATE_PATH = os.path.join(app.static_folder, 'login.html')


def _render_login(**ctx):
    try:
        with open(_LOGIN_TEMPLATE_PATH, 'r', encoding='utf-8') as f:
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


@app.route('/login', methods=['GET', 'POST'])
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


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/api/me', methods=['GET'])
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


@app.route('/api/users', methods=['GET'])
def api_users_list():
    err = _admin_or_error()
    if err:
        return err
    users = User.query.order_by(User.username).all()
    return jsonify([_serialize_user(u) for u in users])


@app.route('/api/users', methods=['POST'])
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


@app.route('/api/users/<int:user_id>', methods=['PUT'])
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


@app.route('/api/users/<int:user_id>', methods=['DELETE'])
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

# API для групп
@app.route('/api/groups', methods=['GET', 'POST'])
def groups_api():
    if request.method == 'GET':
        groups = Group.query.order_by(Group.name).all()
        result = []
        for group in groups:
            group_dict = {
                'id': group.id,
                'name': group.name,
                'color': group.color,
                'parent_id': group.parent_id,
                'servers_count': Server.query.filter_by(group_id=group.id).count(),
                'printers_count': Printer.query.filter_by(group_id=group.id).count(),
                'cameras_count': Camera.query.filter_by(group_id=group.id).count(),
                'routers_count': Router.query.filter_by(group_id=group.id).count()
            }
            result.append(group_dict)
        return jsonify(result)
    
    elif request.method == 'POST':
        data = get_json()
        if not data.get('name'):
            return jsonify({'error': 'Укажите название группы'}), 400
        
        if 'id' in data and data['id']:
            group = Group.query.get(data['id'])
            if not group:
                return jsonify({'error': 'Группа не найдена'}), 404
            group.name = data['name']
            group.color = data.get('color', '#3498db')
            group.parent_id = data.get('parent_id')
            group_id = group.id
        else:
            group = Group(
                name=data['name'],
                color=data.get('color', '#3498db'),
                parent_id=data.get('parent_id')
            )
            db.session.add(group)
            db.session.flush()
            group_id = group.id
        
        db.session.commit()
        return jsonify({'success': True, 'id': group_id})

@app.route('/api/groups/<int:group_id>', methods=['DELETE'])
def delete_group(group_id):
    group = Group.query.get(group_id)
    if not group:
        return jsonify({'error': 'Группа не найдена'}), 404
    
    # Удаляем связи с устройствами
    Server.query.filter_by(group_id=group_id).update({'group_id': None})
    Printer.query.filter_by(group_id=group_id).update({'group_id': None})
    Camera.query.filter_by(group_id=group_id).update({'group_id': None})
    Router.query.filter_by(group_id=group_id).update({'group_id': None})
    
    db.session.delete(group)
    db.session.commit()
    return jsonify({'success': True})

# API для серверов
@app.route('/api/servers', methods=['GET'])
def get_servers():
    servers = Server.query.order_by(Server.name).all()
    statuses = _fetch_statuses_parallel(servers)
    result = []
    for server in servers:
        server_dict = {
            'id': server.id,
            'name': server.name,
            'ip': server.ip,
            'port': server.port,
            'group_id': server.group_id,
            'is_favorite': server.is_favorite,
            'last_seen': server.last_seen.isoformat() if server.last_seen else None,
            'comment': server.comment,
            'created_at': server.created_at.isoformat() if server.created_at else None,
            'rustdesk_id': server.rustdesk_id or '',
            'status': 'online' if statuses.get(server.id) else 'offline'
        }
        
        if server.group:
            server_dict['group_name'] = server.group.name
            server_dict['group_color'] = server.group.color
        
        result.append(server_dict)
    
    return jsonify(result)

@app.route('/api/servers', methods=['POST'])
def add_server():
    data = get_json()
    if not data.get('name') or not data.get('ip'):
        return jsonify({'error': 'Укажите название и IP'}), 400

    port = _normalize_port(data.get('port', 5900))
    if port is None:
        return jsonify({'error': 'Порт должен быть числом от 1 до 65535'}), 400

    # Проверка на дубликат IP
    if Server.query.filter_by(ip=data['ip']).first():
        return jsonify({'error': 'IP адрес уже существует'}), 400
    
    server = Server(
        name=data['name'],
        ip=data['ip'],
        port=port,
        group_id=data.get('group_id'),
        comment=data.get('comment', ''),
        rustdesk_id=(data.get('rustdesk_id') or '').strip(),
    )
    
    db.session.add(server)
    db.session.commit()
    return jsonify({'success': True, 'id': server.id})

@app.route('/api/servers/<int:server_id>', methods=['PUT', 'DELETE'])
def server_api(server_id):
    server = Server.query.get(server_id)
    if not server:
        return jsonify({'error': 'Сервер не найден'}), 404
    
    if request.method == 'PUT':
        data = get_json()
        
        # Проверка на дубликат IP при изменении
        if 'ip' in data and data['ip'] != server.ip:
            if Server.query.filter_by(ip=data['ip']).first():
                return jsonify({'error': 'IP адрес уже существует'}), 400
        
        if 'name' in data:
            server.name = data['name']
        if 'ip' in data:
            server.ip = data['ip']
        if 'port' in data:
            port = _normalize_port(data['port'])
            if port is None:
                return jsonify({'error': 'Порт должен быть числом от 1 до 65535'}), 400
            server.port = port
        if 'group_id' in data:
            server.group_id = data['group_id']
        if 'comment' in data:
            server.comment = data['comment']
        if 'is_favorite' in data:
            server.is_favorite = bool(data['is_favorite'])
        if 'rustdesk_id' in data:
            server.rustdesk_id = (data['rustdesk_id'] or '').strip()
        
        db.session.commit()
        return jsonify({'success': True})
    
    elif request.method == 'DELETE':
        db.session.delete(server)
        db.session.commit()
        return jsonify({'success': True})

# API для принтеров
@app.route('/api/printers', methods=['GET', 'POST'])
def printers_api():
    if request.method == 'GET':
        printers = Printer.query.order_by(Printer.name).all()
        statuses = _fetch_statuses_parallel(printers, is_printer=True)
        result = []
        for printer in printers:
            printer_dict = {
                'id': printer.id,
                'name': printer.name,
                'ip': printer.ip,
                'group_id': printer.group_id,
                'web_interface': printer.web_interface,
                'is_favorite': bool(getattr(printer, 'is_favorite', False)),
                'status': 'online' if statuses.get(printer.id) else 'offline',
                'comment': printer.comment,
                'created_at': printer.created_at.isoformat() if printer.created_at else None
            }
            
            if printer.group:
                printer_dict['group_name'] = printer.group.name
                printer_dict['group_color'] = printer.group.color
            
            result.append(printer_dict)
        
        return jsonify(result)
    
    elif request.method == 'POST':
        data = get_json()
        if not data.get('name') or not data.get('ip'):
            return jsonify({'error': 'Укажите название и IP'}), 400
        
        # Проверка на дубликат IP
        if Printer.query.filter_by(ip=data['ip']).first():
            return jsonify({'error': 'IP адрес уже существует'}), 400
        
        printer = Printer(
            name=data['name'],
            ip=data['ip'],
            group_id=data.get('group_id'),
            web_interface=data.get('web_interface', f"http://{data['ip']}"),
            comment=data.get('comment', ''),
            is_favorite=bool(data.get('is_favorite', False))
        )
        
        db.session.add(printer)
        db.session.commit()
        return jsonify({'success': True, 'id': printer.id})

@app.route('/api/printers/<int:printer_id>', methods=['PUT', 'DELETE'])
def printer_api(printer_id):
    printer = Printer.query.get(printer_id)
    if not printer:
        return jsonify({'error': 'Принтер не найден'}), 404
    
    if request.method == 'PUT':
        data = get_json()
        
        # Проверка на дубликат IP при изменении
        if 'ip' in data and data['ip'] != printer.ip:
            if Printer.query.filter_by(ip=data['ip']).first():
                return jsonify({'error': 'IP адрес уже существует'}), 400
        
        if 'name' in data:
            printer.name = data['name']
        if 'ip' in data:
            printer.ip = data['ip']
        if 'group_id' in data:
            printer.group_id = data['group_id']
        if 'web_interface' in data:
            printer.web_interface = data['web_interface']
        if 'comment' in data:
            printer.comment = data['comment']
        if 'is_favorite' in data:
            printer.is_favorite = bool(data['is_favorite'])
        
        db.session.commit()
        return jsonify({'success': True})
    
    elif request.method == 'DELETE':
        db.session.delete(printer)
        db.session.commit()
        return jsonify({'success': True})

def _web_device_dict(device, status):
    d = {
        'id': device.id,
        'name': device.name,
        'ip': device.ip,
        'port': device.port,
        'group_id': device.group_id,
        'web_interface': device.web_interface,
        'username': device.username,
        'password': '******' if device.password else '',
        'has_password': bool(device.password),
        'is_favorite': bool(getattr(device, 'is_favorite', False)),
        'status': 'online' if status else 'offline',
        'comment': device.comment,
        'created_at': device.created_at.isoformat() if device.created_at else None
    }
    if hasattr(device, 'rtsp_url'):
        d['rtsp_url'] = device.rtsp_url or ''
    if device.group:
        d['group_name'] = device.group.name
        d['group_color'] = device.group.color
    return d

def _apply_password(device, data):
    """Сохраняет пароль, если прислано реальное значение (не маска и не пусто)."""
    pw = data.get('password')
    if isinstance(pw, str) and pw and pw != '******':
        device.password = pw
    elif isinstance(pw, str) and pw == '' and data.get('clear_password'):
        device.password = ''

@app.route('/api/cameras', methods=['GET', 'POST'])
def cameras_api():
    if request.method == 'GET':
        cameras = Camera.query.order_by(Camera.name).all()
        statuses = _fetch_statuses_parallel(cameras, is_web=True)
        return jsonify([_web_device_dict(c, statuses.get(c.id)) for c in cameras])

    data = get_json()
    if not data.get('name') or not data.get('ip'):
        return jsonify({'error': 'Укажите название и IP'}), 400
    if Camera.query.filter_by(ip=data['ip']).first():
        return jsonify({'error': 'IP адрес уже существует'}), 400

    camera = Camera(
        name=data['name'],
        ip=data['ip'],
        port=_normalize_port(data.get('port'), 80) or 80,
        group_id=data.get('group_id'),
        web_interface=data.get('web_interface', f"http://{data['ip']}"),
        rtsp_url=data.get('rtsp_url', ''),
        username=data.get('username', ''),
        password=data.get('password', ''),
        comment=data.get('comment', ''),
        is_favorite=bool(data.get('is_favorite', False))
    )
    db.session.add(camera)
    db.session.commit()
    return jsonify({'success': True, 'id': camera.id})

@app.route('/api/cameras/<int:camera_id>', methods=['PUT', 'DELETE'])
def camera_api(camera_id):
    camera = Camera.query.get(camera_id)
    if not camera:
        return jsonify({'error': 'Камера не найдена'}), 404

    if request.method == 'PUT':
        data = get_json()
        if 'ip' in data and data['ip'] != camera.ip:
            if Camera.query.filter_by(ip=data['ip']).first():
                return jsonify({'error': 'IP адрес уже существует'}), 400
        for field in ('name', 'ip', 'web_interface', 'rtsp_url', 'username', 'comment'):
            if field in data:
                setattr(camera, field, data[field])
        if 'port' in data:
            port = _normalize_port(data.get('port'), 80)
            if port:
                camera.port = port
        if 'group_id' in data:
            camera.group_id = data['group_id']
        if 'is_favorite' in data:
            camera.is_favorite = bool(data['is_favorite'])
        _apply_password(camera, data)
        db.session.commit()
        return jsonify({'success': True})

    db.session.delete(camera)
    db.session.commit()
    return jsonify({'success': True})

@app.route('/api/routers', methods=['GET', 'POST'])
def routers_api():
    if request.method == 'GET':
        routers = Router.query.order_by(Router.name).all()
        statuses = _fetch_statuses_parallel(routers, is_web=True)
        return jsonify([_web_device_dict(r, statuses.get(r.id)) for r in routers])

    data = get_json()
    if not data.get('name') or not data.get('ip'):
        return jsonify({'error': 'Укажите название и IP'}), 400
    if Router.query.filter_by(ip=data['ip']).first():
        return jsonify({'error': 'IP адрес уже существует'}), 400

    router = Router(
        name=data['name'],
        ip=data['ip'],
        port=_normalize_port(data.get('port'), 80) or 80,
        group_id=data.get('group_id'),
        web_interface=data.get('web_interface', f"http://{data['ip']}"),
        username=data.get('username', ''),
        password=data.get('password', ''),
        comment=data.get('comment', ''),
        is_favorite=bool(data.get('is_favorite', False))
    )
    db.session.add(router)
    db.session.commit()
    return jsonify({'success': True, 'id': router.id})

@app.route('/api/routers/<int:router_id>', methods=['PUT', 'DELETE'])
def router_api(router_id):
    router = Router.query.get(router_id)
    if not router:
        return jsonify({'error': 'Роутер не найден'}), 404

    if request.method == 'PUT':
        data = get_json()
        if 'ip' in data and data['ip'] != router.ip:
            if Router.query.filter_by(ip=data['ip']).first():
                return jsonify({'error': 'IP адрес уже существует'}), 400
        for field in ('name', 'ip', 'web_interface', 'username', 'comment'):
            if field in data:
                setattr(router, field, data[field])
        if 'port' in data:
            port = _normalize_port(data.get('port'), 80)
            if port:
                router.port = port
        if 'group_id' in data:
            router.group_id = data['group_id']
        if 'is_favorite' in data:
            router.is_favorite = bool(data['is_favorite'])
        _apply_password(router, data)
        db.session.commit()
        return jsonify({'success': True})

    db.session.delete(router)
    db.session.commit()
    return jsonify({'success': True})

# --- RTSP->HLS просмотр камер в браузере ---
_HLS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'instance', 'hls')
_HLS_LOCK = threading.Lock()
_HLS_PROCESSES = {}  # camera_id -> Popen


def _hls_ffmpeg_available():
    try:
        r = subprocess.run(['ffmpeg', '-version'], capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def _rtsp_url_with_creds(camera):
    """RTSP URL с подставленными логином/паролем камеры (если заданы и не в URL)."""
    url = (camera.rtsp_url or '').strip()
    if not url:
        return url
    # Если credentials уже встроены в URL (user@ или user:pass@) — не дублируем
    if re.search(r'://[^/@\s]+@', url):
        return url
    user = (camera.username or '').strip()
    pwd = camera.password or ''
    if not user and not pwd:
        return url
    creds = quote(user, safe='')
    if pwd:
        creds += ':' + quote(pwd, safe='')
    if '://' in url:
        scheme, rest = url.split('://', 1)
        url = f'{scheme}://{creds}@{rest}'
    return url


@app.route('/api/camera/stream/<int:camera_id>', methods=['POST', 'DELETE'])
def camera_hls(camera_id):
    """Запуск/остановка FFmpeg-процесса, перекодирующего RTSP камеры в HLS."""
    camera = Camera.query.get(camera_id)
    if not camera:
        return jsonify({'error': 'Камера не найдена'}), 404

    if request.method == 'DELETE':
        with _HLS_LOCK:
            p = _HLS_PROCESSES.pop(camera_id, None)
        if p:
            try:
                p.terminate()
            except Exception:
                pass
        return jsonify({'success': True})

    if not camera.rtsp_url:
        return jsonify({'error': 'RTSP поток не указан'}), 400
    if not _hls_ffmpeg_available():
        return jsonify({'error': 'FFmpeg недоступен на сервере'}), 500

    rtsp_url = _rtsp_url_with_creds(camera)

    with _HLS_LOCK:
        existing = _HLS_PROCESSES.get(camera_id)
        if existing and existing.poll() is None:
            return jsonify({'playlist': f'/api/camera/stream/{camera_id}/playlist.m3u8'})

        outdir = os.path.join(_HLS_DIR, str(camera_id))
        os.makedirs(outdir, exist_ok=True)
        for f in os.listdir(outdir):
            try:
                os.remove(os.path.join(outdir, f))
            except OSError:
                pass

        cmd = [
            'ffmpeg', '-hide_banner', '-loglevel', 'error',
            '-rtsp_transport', 'tcp',
            '-i', rtsp_url,
            '-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'zerolatency',
            '-g', '30', '-sc_threshold', '0',
            '-c:a', 'aac', '-b:a', '96k', '-ac', '1',
            '-f', 'hls', '-hls_time', '2', '-hls_list_size', '6',
            '-hls_flags', 'delete_segments+omit_endlist',
            os.path.join(outdir, 'playlist.m3u8')
        ]
        try:
            log_file = open(os.path.join(outdir, 'ffmpeg.log'), 'ab')
            p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=log_file)
        except OSError as e:
            return jsonify({'error': f'Ошибка запуска FFmpeg: {e}'}), 500

        # Даём FFmpeg пару секунд на подключение к камере; если он сразу упал
        # (неверный логин/пароль, недоступный RTSP) — возвращаем понятную ошибку.
        time.sleep(2)
        if p.poll() is not None:
            try:
                log_file.close()
            except Exception:
                pass
            _HLS_PROCESSES.pop(camera_id, None)
            return jsonify({'error': 'Не удалось подключиться к камере (проверьте логин/пароль и RTSP-адрес)'}), 502

        # Ждём появления playlist.m3u8: ffmpeg создаёт его не сразу, а после
        # подключения к камере и набора первых сегментов. Возвращаем 200 только
        # когда плейлист реально существует, иначе hls.js получит 404 и упадёт.
        playlist_path = os.path.join(outdir, 'playlist.m3u8')
        waited = 0
        while not os.path.exists(playlist_path) and waited < 10:
            if p.poll() is not None:
                break
            time.sleep(0.25)
            waited += 0.25
        if not os.path.exists(playlist_path):
            try:
                p.terminate()
            except Exception:
                pass
            try:
                log_file.close()
            except Exception:
                pass
            _HLS_PROCESSES.pop(camera_id, None)
            return jsonify({'error': 'Не удалось запустить трансляцию (проверьте RTSP-адрес и доступность камеры)'}), 502

        _HLS_PROCESSES[camera_id] = p

    return jsonify({'playlist': f'/api/camera/stream/{camera_id}/playlist.m3u8'})


@app.route('/api/camera/stream/<int:camera_id>/<path:filename>', methods=['GET'])
def camera_hls_file(camera_id, filename):
    safe = os.path.basename(filename)
    if not safe or not (safe.endswith('.m3u8') or safe.endswith('.ts')):
        return jsonify({'error': 'Not Found'}), 404
    path = os.path.join(_HLS_DIR, str(camera_id), safe)
    if not os.path.exists(path):
        return jsonify({'error': 'Not Found'}), 404
    if safe.endswith('.m3u8'):
        return send_file(path, mimetype='application/vnd.apple.mpegurl')
    return send_file(path, mimetype='video/mp2t')


_CAMERA_VIEW_HTML = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>{{ camera.name }} — просмотр</title>
<style>
  body{margin:0;background:#000;color:#fff;font-family:system-ui,sans-serif}
  .wrap{position:fixed;inset:0;display:flex;flex-direction:column}
  #bar{display:flex;align-items:center;gap:12px;padding:8px 14px;background:#111;font-size:13px}
  #status{color:#aaa;font-size:12px}
  .btn{background:#333;border:1px solid #555;color:#fff;padding:6px 14px;border-radius:6px;cursor:pointer}
  .btn:hover{background:#444}
  #stage{flex:1;position:relative}
  video{width:100%;height:100%;background:#000}
  #err{display:none;position:absolute;inset:0;align-items:center;justify-content:center;color:#f66;text-align:center;padding:20px;box-sizing:border-box}
</style>
</head>
<body>
<div class="wrap">
  <div id="bar">
    <strong>{{ camera.name }}</strong>
    <span style="opacity:.6">{{ camera.ip }}</span>
    <span id="status">Подключение...</span>
    <span style="flex:1"></span>
    <button class="btn" onclick="togglePlay()">Пауза</button>
    <button class="btn" id="btnSound" onclick="toggleSound()">Включить звук</button>
    <button class="btn" onclick="window.close()">Закрыть</button>
  </div>
  <div id="stage">
    <video id="video" controls autoplay muted></video>
    <div id="err"></div>
  </div>
</div>
<script src="/hls.min.js"></script>
<script>
  const video = document.getElementById('video');
  const statusEl = document.getElementById('status');
  const errEl = document.getElementById('err');
  const btnSound = document.getElementById('btnSound');
  const playlist = '/api/camera/stream/{{ camera.id }}/playlist.m3u8';
  let hls = null;

  function showError(msg) {
    errEl.textContent = msg;
    errEl.style.display = 'flex';
    statusEl.textContent = 'Ошибка';
  }

  async function start() {
    try {
      const r = await fetch('/api/camera/stream/{{ camera.id }}', { method: 'POST' });
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        throw new Error(d.error || 'Не удалось запустить поток');
      }
    } catch (e) {
      showError(e.message);
      return;
    }

    if (window.Hls && Hls.isSupported()) {
      hls = new Hls({ liveDurationInfinity: true });
      hls.on(Hls.Events.MANIFEST_PARSED, () => { statusEl.textContent = 'Live'; });
      hls.on(Hls.Events.ERROR, (evt, data) => {
        if (data.fatal) {
          hls.destroy();
          showError('Поток не воспроизводится (недоступен или требует авторизации камеры)');
        }
      });
      hls.loadSource(playlist);
      hls.attachMedia(video);
    } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
      video.src = playlist;
      statusEl.textContent = 'Live';
    } else {
      showError('Браузер не поддерживает HLS');
    }
  }

  function togglePlay() {
    if (video.paused) { video.play(); statusEl.textContent = 'Live'; }
    else { video.pause(); statusEl.textContent = 'Пауза'; }
  }

  function toggleSound() {
    video.muted = !video.muted;
    btnSound.textContent = video.muted ? 'Включить звук' : 'Выключить звук';
    if (!video.muted) { video.volume = 1; }
  }

  window.addEventListener('beforeunload', () => {
    fetch('/api/camera/stream/{{ camera.id }}', { method: 'DELETE', keepalive: true });
  });

  start();
</script>
</body>
</html>"""


@app.route('/camera_view/<int:camera_id>')
def camera_view_page(camera_id):
    camera = Camera.query.get(camera_id)
    if not camera:
        abort(404)
    return render_template_string(_CAMERA_VIEW_HTML, camera=camera)


@app.route('/api/connect/<int:server_id>', methods=['POST'])
def connect_vnc(server_id):
    """Подключение к VNC серверу"""
    server = Server.query.get(server_id)
    if not server:
        return jsonify({'success': False, 'message': 'Сервер не найден'}), 404
    
    vnc_path, client_name = find_vnc_client()
    
    if not vnc_path:
        return jsonify({
            'success': False,
            'message': 'VNC клиент не найден',
            'instructions': [
                'Установите один из VNC клиентов:',
                '- TightVNC: http://www.tightvnc.com/download.php',
                '- RealVNC Viewer: https://www.realvnc.com/en/connect/download/viewer/',
                '',
                'Или подключитесь вручную:',
                f'Адрес: {server.ip}:{server.port}'
            ]
        })
    
    try:
        if platform.system() == 'Darwin':
            if 'Screen Sharing' in vnc_path:
                subprocess.Popen(['open', f"vnc://{server.ip}:{server.port}"])
            else:
                subprocess.Popen([vnc_path, f"{server.ip}:{server.port}"])
        else:
            subprocess.Popen([vnc_path, f"{server.ip}:{server.port}"])
        
        return jsonify({
            'success': True,
            'message': f'Открываю подключение к {server.ip}:{server.port} через {client_name}'
        })
        
    except Exception as e:
        return jsonify({
            'success': False,
            'message': f'Ошибка при запуске VNC клиента: {str(e)}',
            'manual_connection': f"Вы можете подключиться вручную: {server.ip}:{server.port}"
        })


@app.route('/api/scan', methods=['POST'])
def scan_network():
    """Сканирование сети для поиска VNC серверов и принтеров"""
    data = request.json or {}
    range_input = data.get('range', '').strip()
    
    if not range_input:
        return jsonify({'error': 'Укажите диапазон для сканирования'}), 400
    
    try:
        # Парсим диапазон (поддержка CIDR и простых IP)
        if '/' in range_input:
            network = ipaddress.ip_network(range_input, strict=False)
            ips = [str(ip) for ip in network.hosts()]
        else:
            # Одиночный IP
            ip = ipaddress.ip_address(range_input)
            ips = [str(ip)]
    except ValueError as e:
        return jsonify({'error': f'Неверный формат диапазона: {str(e)}'}), 400
    
    # Ограничиваем количество IP для безопасности
    if len(ips) > 254:
        return jsonify({'error': 'Слишком большой диапазон (максимум 254 адреса)'}), 400
    
    # Логируем для отладки
    app.logger.info(f'Сканирование диапазона {range_input}: {len(ips)} IP адресов')
    if ips:
        app.logger.info(f'Первый IP: {ips[0]}, Последний IP: {ips[-1]}')
    
    results = []
    
    def get_printer_info(ip, port):
        """Получить подробную информацию о принтере"""
        try:
            import urllib.request
            import urllib.error
            import re
            import json
            
            info = {
                'model': None,
                'serial': None,
                'status': None,
                'toner_level': None,
                'ink_level': None,
                'page_count': None,
                'manufacturer': None
            }
            
            # Пробуем получить информацию с веб-интерфейса
            url = f'http{"s" if port == 443 else ""}://{ip}:{port}'
            
            # Попытка получить основную страницу
            try:
                req = urllib.request.Request(url, method='GET')
                with urllib.request.urlopen(req, timeout=5) as response:
                    content = response.read(10000).decode('utf-8', errors='ignore')
                    
                    # Поиск модели в содержимом
                    model_patterns = [
                        r'<title[^>]*>([^<]+)</title>',
                        r'Model[:\s]+([^\n<]+)',
                        r'Printer[:\s]+([^\n<]+)',
                        r'(HP|Canon|Epson|Brother|Xerox|Lexmark|Samsung|Kyocera|Ricoh|Panasonic|Sharp|Toshiba|Konica|Minolta|OKI|Dell|Fuji|Zebra|Dymo)[\s-]+([A-Z0-9\-]+)'
                    ]
                    
                    for pattern in model_patterns:
                        match = re.search(pattern, content, re.IGNORECASE)
                        if match:
                            model = match.group(1).strip()
                            if len(model) > 3:  # Исключаем короткие совпадения
                                info['model'] = model
                                break
                    
                    # Определение производителя
                    manufacturer_patterns = {
                        'hp': 'HP', 'canon': 'Canon', 'epson': 'Epson',
                        'brother': 'Brother', 'xerox': 'Xerox', 'lexmark': 'Lexmark',
                        'samsung': 'Samsung', 'kyocera': 'Kyocera', 'ricoh': 'Ricoh',
                        'panasonic': 'Panasonic', 'sharp': 'Sharp', 'toshiba': 'Toshiba',
                        'konica': 'Konica', 'minolta': 'Minolta', 'oki': 'OKI',
                        'dell': 'Dell', 'fuji': 'Fuji', 'zebra': 'Zebra', 'dymo': 'Dymo'
                    }
                    
                    content_lower = content.lower()
                    for key, manufacturer in manufacturer_patterns.items():
                        if key in content_lower:
                            info['manufacturer'] = manufacturer
                            break
                    
                    # Поиск серийного номера
                    serial_patterns = [
                        r'Serial[:\s]+([A-Z0-9\-]+)',
                        r'S/N[:\s]+([A-Z0-9\-]+)',
                        r'SerialNumber["\']:\s*["\']([^"\']+)["\']',
                        r'SN["\']:\s*["\']([^"\']+)["\']'
                    ]
                    
                    for pattern in serial_patterns:
                        match = re.search(pattern, content, re.IGNORECASE)
                        if match:
                            info['serial'] = match.group(1).strip()
                            break
                    
                    # Поиск информации о тонере/чернилах
                    toner_patterns = [
                        r'Toner[:\s]+([0-9]+)%',
                        r'Black[:\s]+([0-9]+)%',
                        r'Ink[:\s]+([0-9]+)%',
                        r'Cartridge[:\s]+([0-9]+)%'
                    ]
                    
                    for pattern in toner_patterns:
                        match = re.search(pattern, content, re.IGNORECASE)
                        if match:
                            level = int(match.group(1))
                            if 'toner' in pattern.lower() or 'black' in pattern.lower():
                                info['toner_level'] = level
                            else:
                                info['ink_level'] = level
                            break
                    
                    # Поиск счетчика страниц
                    page_patterns = [
                        r'Page\s+Count[:\s]+([0-9,]+)',
                        r'Pages[:\s]+([0-9,]+)',
                        r'Counter[:\s]+([0-9,]+)'
                    ]
                    
                    for pattern in page_patterns:
                        match = re.search(pattern, content, re.IGNORECASE)
                        if match:
                            info['page_count'] = match.group(1).replace(',', '')
                            break
                    
                    # Поиск статуса
                    status_patterns = [
                        r'Status[:\s]+([^\n<]+)',
                        r'Ready', r'Online', r'Offline', r'Error', r'Busy'
                    ]
                    
                    for pattern in status_patterns:
                        if isinstance(pattern, str):
                            if pattern.lower() in content_lower:
                                info['status'] = pattern
                                break
                        else:
                            match = re.search(pattern, content, re.IGNORECASE)
                            if match:
                                info['status'] = match.group(1).strip()
                                break
                    
            except Exception as e:
                app.logger.debug(f'Ошибка получения основной страницы принтера {ip}:{port}: {e}')
            
            # Пробуем SNMP для дополнительной информации
            try:
                import subprocess
                # Попытка получить системную информацию через SNMP
                result = subprocess.run([
                    'snmpget', '-v2c', '-c', 'public', ip,
                    '1.3.6.1.2.1.1.1.0',  # sysDescr
                    '1.3.6.1.2.1.1.5.0',  # sysName
                    '1.3.6.1.2.1.1.6.0'   # sysLocation
                ], capture_output=True, text=True, timeout=3)
                
                if result.returncode == 0:
                    snmp_data = result.stdout
                    
                    # Парсинг SNMP ответа
                    if 'sysDescr' in snmp_data and not info['model']:
                        desc_match = re.search(r'String:\s*(.+)', snmp_data)
                        if desc_match:
                            info['model'] = desc_match.group(1).strip()
                    
                    if 'sysName' in snmp_data and not info['model']:
                        name_match = re.search(r'String:\s*(.+)', snmp_data.split('sysName')[1])
                        if name_match:
                            info['model'] = name_match.group(1).strip()
                            
            except Exception as e:
                app.logger.debug(f'SNMP недоступен для {ip}: {e}')
            
            # Удаляем None значения
            info = {k: v for k, v in info.items() if v is not None}
            
            return info
            
        except Exception as e:
            app.logger.debug(f'Ошибка получения информации о принтере {ip}: {e}')
            return {}
    
    def get_hostname(ip):
        """Получить имя хоста по IP через reverse DNS"""
        try:
            import socket
            hostname = socket.gethostbyaddr(ip)[0]
            return hostname
        except Exception:
            return ip
    
    def check_ip(ip):
        """Проверка одного IP адреса"""
        try:
            app.logger.debug(f'Начало проверки IP: {ip}')
            
            # Сначала проверяем ping
            if not ping_host(ip):
                app.logger.debug(f'IP {ip} не отвечает на ping')
                return None
            
            app.logger.debug(f'IP {ip} отвечает на ping, проверяем порты')
            found_devices = []
            
            # Проверяем VNC (порт 5900)
            if check_port(ip, 5900):
                hostname = get_hostname(ip)
                found_devices.append({
                    'type': 'server',
                    'ip': ip,
                    'port': 5900,
                    'name': f'VNC-{hostname}',
                    'status': 'online',
                    'protocols': ['VNC']
                })
                app.logger.info(f'Найден VNC сервер: {ip}:5900')
            
            # Проверяем веб-интерфейсы принтеров (порты 80, 443, 9100, 631)
            for port in [80, 443, 9100, 631]:
                if check_port(ip, port):
                    app.logger.debug(f'IP {ip} имеет открытый порт {port}, проверяем на принтер')
                    # Дополнительная проверка на принтер
                    if is_likely_printer(ip, port):
                        hostname = get_hostname(ip)
                        printer_info = get_printer_info(ip, port)
                        
                        # Используем модель из printer_info, если доступна
                        display_name = printer_info.get('model', hostname)
                        
                        device_data = {
                            'type': 'printer',
                            'ip': ip,
                            'port': port,
                            'name': display_name,
                            'status': 'online',
                            'web_interface': f'http{"s" if port == 443 else ""}://{ip}:{port}',
                            'protocols': [{
                                80: 'HTTP', 443: 'HTTPS', 9100: 'RAW', 631: 'IPP', 515: 'LPR'
                            }.get(port, 'TCP')]
                        }
                        
                        # Добавляем подробную информацию, если доступна
                        device_data.update(printer_info)
                        
                        found_devices.append(device_data)
                        app.logger.info(f'Найден принтер: {ip}:{port} - {display_name}')
            
            # Ищем камеры и роутеры (если на IP не найден принтер)
            if not any(d['type'] == 'printer' for d in found_devices):
                hostname = get_hostname(ip)
                cam = detect_camera(ip, hostname)
                if cam:
                    found_devices.append(cam)
                    app.logger.info(f'Найдена камера: {ip} - {cam.get("name")}')
                else:
                    router = detect_router(ip, hostname)
                    if router:
                        found_devices.append(router)
                        app.logger.info(f'Найден роутер: {ip} - {router.get("name")}')

            if found_devices:
                app.logger.debug(f'IP {ip}: найдено {len(found_devices)} устройств')
            else:
                app.logger.debug(f'IP {ip}: устройств не найдено')
            
            return found_devices if found_devices else None
            
        except Exception as e:
            app.logger.error(f'Ошибка при проверке IP {ip}: {e}')
            return None
    
    def ping_host(ip):
        """Проверка доступности хоста"""
        try:
            if platform.system().lower() == 'windows':
                cmd = ['ping', '-n', '1', '-w', '1000', ip]
            else:
                cmd = ['ping', '-c', '1', '-W', '1', ip]
            
            app.logger.debug(f'Проверка ping для {ip}: {" ".join(cmd)}')
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
            app.logger.debug(f'Ping {ip}: returncode={result.returncode}, stdout={result.stdout[:100]}')
            return result.returncode == 0
        except FileNotFoundError:
            # Если ping недоступен (например в Docker без ping), пропускаем проверку
            app.logger.warning(f'Ping недоступен, пропускаем проверку для {ip}')
            return True  # Считаем что хост доступен, переходим к проверке портов
        except Exception as e:
            app.logger.error(f'Ошибка ping для {ip}: {e}')
            return False
    
    open_cache = {}

    def check_port(ip, port):
        """Проверка открытого порта (с кэшем)"""
        key = (ip, int(port))
        if key in open_cache:
            return open_cache[key]
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2)
            result = sock.connect_ex((ip, port))
            sock.close()
            open_cache[key] = result == 0
            app.logger.debug(f'Проверка порта {ip}:{port} - {"открыт" if result == 0 else "закрыт"}')
            return open_cache[key]
        except Exception as e:
            app.logger.debug(f'Ошибка проверки порта {ip}:{port}: {e}')
            open_cache[key] = False
            return False

    def _cached_open_ports(ip):
        """Открытые порты IP из уже выполненного сканирования (без новых запросов)."""
        return {p for (i, p), v in open_cache.items() if i == ip and v}

    def _camera_protocols(ip):
        ports = _cached_open_ports(ip)
        protos = []
        if 554 in ports:
            protos.append('RTSP')
        if 80 in ports or 8080 in ports:
            protos.append('HTTP')
        if 443 in ports:
            protos.append('HTTPS')
        if 8000 in ports:
            protos.append('Hikvision SDK')
        if 37777 in ports:
            protos.append('Dahua SDK')
        return protos

    def _router_protocols(ip):
        ports = _cached_open_ports(ip)
        protos = []
        if 80 in ports or 8080 in ports:
            protos.append('HTTP')
        if 443 in ports or 8443 in ports:
            protos.append('HTTPS')
        if 22 in ports:
            protos.append('SSH')
        if 23 in ports:
            protos.append('Telnet')
        if 161 in ports:
            protos.append('SNMP')
        if 1900 in ports:
            protos.append('UPnP')
        if 7547 in ports:
            protos.append('TR-069')
        return protos

    def is_likely_printer(ip, port):
        """Улучшенная проверка на принтер"""
        if port in (9100, 631, 515):
            return True
        try:
            import urllib.request

            url = f'http{"s" if port == 443 else ""}://{ip}:{port}'
            req = urllib.request.Request(url, method='GET')

            with urllib.request.urlopen(req, timeout=5) as response:
                headers = dict(response.headers)
                content = response.read(5000).decode('utf-8', errors='ignore').lower()

                printer_keywords = [
                    'printer', 'hp', 'canon', 'epson', 'brother', 'xerox',
                    'lexmark', 'samsung', 'kyocera', 'ricoh', 'panasonic',
                    'sharp', 'toshiba', 'konica', 'minolta', 'oki',
                    'dell', 'fuji', 'zebra', 'dymo'
                ]

                server = headers.get('Server', '').lower()
                content_type = headers.get('Content-Type', '').lower()
                header_match = any(keyword in server for keyword in printer_keywords)
                content_type_match = any(keyword in content_type for keyword in printer_keywords)
                title_match = '<title>' in content and any(keyword in content for keyword in printer_keywords)
                path_match = any(path in content for path in ['/printer', '/status', '/main', '/device', '/web'])
                is_printer = header_match or content_type_match or title_match or path_match

                if is_printer:
                    app.logger.info(
                        f'Найден принтер {ip}:{port} - признаки: header={header_match}, '
                        f'content_type={content_type_match}, title={title_match}, path={path_match}'
                    )
                return is_printer

        except Exception as e:
            app.logger.debug(f'Ошибка проверки принтера {ip}:{port}: {e}')
            return False

    web_probe_cache = {}

    def get_web(ip, port):
        """HTTP(S) GET: возвращает (headers_lower, content_lower, status) с кэшем."""
        key = (ip, int(port))
        if key in web_probe_cache:
            return web_probe_cache[key]
        result = (None, None, None)
        try:
            import urllib.request
            import urllib.error
            scheme = 'https' if port == 443 else 'http'
            req = urllib.request.Request(f'{scheme}://{ip}:{port}/', method='GET', headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=4) as response:
                raw = response.read(20000)
                try:
                    content = raw.decode('utf-8', errors='ignore').lower()
                except Exception:
                    content = ''
                headers = {k.lower(): (v or '').lower() for k, v in response.headers.items()}
                result = (headers, content, response.status)
        except urllib.error.HTTPError as e:
            result = ({'status': 'HTTP', 'server': ''}, str(e.code), e.code)
        except Exception as e:
            app.logger.debug(f'get_web {ip}:{port} failed: {e}')
        web_probe_cache[key] = result
        return result

    def banner_grab(ip, port, read=True):
        """Чтение TCP-баннера с порта."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2.5)
            sock.connect((ip, port))
            if read:
                data = sock.recv(1024)
                sock.close()
                return data.decode('utf-8', errors='ignore')
            sock.close()
            return ''
        except Exception:
            return ''

    def rtsp_probe(ip, port=554):
        """Проверка RTSP-сервера: отправка OPTIONS, возврат ответа."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2.5)
            sock.connect((ip, port))
            sock.send(f'OPTIONS rtsp://{ip}:{port}/ RTSP/1.0\r\nCSeq: 1\r\n\r\n'.encode())
            data = sock.recv(4096).decode('utf-8', errors='ignore')
            sock.close()
            return data if 'RTSP/1.0' in data else ''
        except Exception:
            return ''

    CAMERA_KEYWORDS = [
        'hikvision', 'dahua', 'dvr', 'nvr', 'ipcam', 'ip camera', 'webcam',
        'onvif', 'rtsp', 'reolink', 'amcrest', 'axis', 'xiaomi', 'smartvision',
        'ezviz', 'uniview', 'sunell', 'tp-camera', 'netvue', 'camera', 'wisenet', 'samsung-camera'
    ]
    CAMERA_SERVER_HEADERS = ['dvrds-webs', 'ipcamera', 'hikvision', 'dahua', 'webcam']

    ROUTER_KEYWORDS = [
        'mikrotik', 'routeros', 'winbox', 'tp-link', 'tplink', 'd-link', 'dlink',
        'asus', 'keenetic', 'zyxel', 'unifi', 'ubiquiti', 'cisco', 'openwrt', 'luci',
        'pfsense', 'routers', 'netgear', 'huawei', 'juniper', 'fortinet', 'draytek',
        'fritz', 'totolink', 'mikrotik router', 'zywall', 'vodafone', 'linksys'
    ]

    def _web_scores(headers, content):
        cam_score = 0
        router_score = 0
        text = (content or '')
        server = (headers or {}).get('server', '')
        for kw in CAMERA_KEYWORDS:
            if kw in server or kw in text:
                cam_score += 1
        for h in CAMERA_SERVER_HEADERS:
            if h in server:
                cam_score += 2
        for kw in ROUTER_KEYWORDS:
            if kw in server or kw in text:
                router_score += 1
        return cam_score, router_score

    def _camera_from_web(ip, hostname, port, web):
        headers, content, _ = web
        cam_score, router_score = _web_scores(headers, content)
        if cam_score > router_score and cam_score > 0:
            name = hostname
            for marker in ('hikvision', 'dahua', 'reolink', 'amcrest', 'axis', 'uniview', 'wisenet', 'ezviz'):
                idx = content.find(marker)
                if idx != -1:
                    snippet = content[max(0, idx - 40):idx + 60].split('<')[0].strip()
                    if snippet:
                        name = snippet if len(snippet) < 60 else snippet[:60]
                        break
            return {
                'type': 'camera',
                'ip': ip,
                'port': port,
                'name': name,
                'status': 'online',
                'web_interface': f'http{"s" if port == 443 else ""}://{ip}:{port}',
                'protocols': _camera_protocols(ip)
            }
        return None

    def detect_camera(ip, hostname):
        """Поиск камеры видеонаблюдения по RTSP/HTTP/ONVIF/SDK/MJPEG."""
        result = None

        rtsp_data = rtsp_probe(ip) if check_port(ip, 554) else ''
        if rtsp_data:
            name = hostname
            server_line = [ln for ln in rtsp_data.splitlines() if ln.lower().startswith('server:')]
            if server_line:
                model = server_line[0].split(':', 1)[1].strip()
                if model:
                    name = model if len(model) < 60 else model[:60]
            result = {
                'type': 'camera',
                'ip': ip,
                'port': 554,
                'name': name,
                'status': 'online',
                'rtsp_url': f'rtsp://{ip}:554/',
                'web_interface': '',
                'protocols': _camera_protocols(ip)
            }
            return result

        for port in (80, 443, 8080):
            if check_port(ip, port):
                web = get_web(ip, port)
                cam = _camera_from_web(ip, hostname, port, web)
                if cam:
                    return cam

        if check_port(ip, 8000):  # Hikvision SDK / IPC
            b = banner_grab(ip, 8000)
            if 'hikvision' in b.lower() or 'ipc' in b.lower():
                return {'type': 'camera', 'ip': ip, 'port': 8000, 'name': 'Hikvision IPC', 'status': 'online', 'rtsp_url': f'rtsp://{ip}:554/', 'web_interface': '', 'protocols': _camera_protocols(ip)}
            if b:
                return {'type': 'camera', 'ip': ip, 'port': 8000, 'name': hostname, 'status': 'online', 'web_interface': '', 'protocols': _camera_protocols(ip)}

        if check_port(ip, 37777):  # Dahua SDK
            b = banner_grab(ip, 37777)
            if b:
                return {'type': 'camera', 'ip': ip, 'port': 37777, 'name': hostname, 'status': 'online', 'web_interface': '', 'protocols': _camera_protocols(ip)}

        return result

    def _router_from_web(ip, hostname, port, web):
        headers, content, _ = web
        cam_score, router_score = _web_scores(headers, content)
        if router_score > 0 and router_score >= cam_score:
            name = hostname
            for marker in ('mikrotik', 'routeros', 'tp-link', 'd-link', 'keenetic', 'unifi', 'openwrt', 'asus', 'cisco'):
                idx = content.find(marker)
                if idx != -1:
                    snippet = content[max(0, idx - 30):idx + 50].split('<')[0].strip()
                    if snippet:
                        name = snippet if len(snippet) < 60 else snippet[:60]
                        break
            return {
                'type': 'router',
                'ip': ip,
                'port': port,
                'name': name,
                'status': 'online',
                'web_interface': f'http{"s" if port == 443 else ""}://{ip}:{port}',
                'protocols': _router_protocols(ip)
            }
        return None

    def detect_router(ip, hostname):
        """Поиск роутера/сетевого оборудования по веб/SSH/Telnet/SNMP/UPnP/TR-069."""
        for port in (80, 443, 8080, 8443):
            if check_port(ip, port):
                web = get_web(ip, port)
                router = _router_from_web(ip, hostname, port, web)
                if router:
                    return router

        if check_port(ip, 22):
            banner = banner_grab(ip, 22)
            if banner and 'SSH-2.0' in banner:
                name = hostname
                if 'openwrt' in banner.lower() or 'dropbear' in banner.lower() or 'routeros' in banner.lower():
                    name = banner.split('\r\n')[0].strip()
                return {'type': 'router', 'ip': ip, 'port': 22, 'name': name, 'status': 'online', 'web_interface': '', 'protocols': _router_protocols(ip)}

        if check_port(ip, 23):
            banner = banner_grab(ip, 23)
            if banner and any(k in banner.lower() for k in ('cisco', 'routeros', 'telnet', 'dlink', 'huawei', 'zyxel')):
                return {'type': 'router', 'ip': ip, 'port': 23, 'name': banner.split('\r\n')[0].strip() or hostname, 'status': 'online', 'web_interface': '', 'protocols': _router_protocols(ip)}

        if check_port(ip, 7547):  # TR-069
            return {'type': 'router', 'ip': ip, 'port': 7547, 'name': hostname, 'status': 'online', 'web_interface': '', 'protocols': _router_protocols(ip)}

        try:
            import subprocess
            r = subprocess.run(
                ['snmpget', '-v2c', '-c', 'public', '-t', '1', '-r', '0', ip, '1.3.6.1.2.1.1.1.0'],
                capture_output=True, text=True, timeout=3
            )
            if r.returncode == 0 and ('STRING' in r.stdout or 'OID' in r.stdout):
                desc = r.stdout.strip()
                if any(k in desc.lower() for k in ('cisco', 'mikrotik', 'routeros', 'huawei', 'zyxel', 'router', 'd-link', 'tp-link', 'dlink', 'switch')):
                    return {'type': 'router', 'ip': ip, 'port': 161, 'name': desc[:60], 'status': 'online', 'web_interface': '', 'protocols': _router_protocols(ip)}
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass

        try:
            import socket as _s
            msg = ('M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n'
                   'MAN: "ssdp:discover"\r\nMX: 1\r\nST: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n\r\n').encode()
            s = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
            s.settimeout(2)
            s.sendto(msg, (ip, 1900))
            data, _ = s.recvfrom(2048)
            s.close()
            resp = data.decode('utf-8', errors='ignore').lower()
            if 'internetgatewaydevice' in resp or 'location:' in resp:
                return {'type': 'router', 'ip': ip, 'port': 1900, 'name': hostname, 'status': 'online', 'web_interface': '', 'protocols': _router_protocols(ip)}
        except Exception:
            pass

        return None

    # Параллельная проверка IP адресов
    checked_count = 0
    responsive_count = 0
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
        future_to_ip = {executor.submit(check_ip, ip): ip for ip in ips}
        
        for future in concurrent.futures.as_completed(future_to_ip):
            try:
                checked_count += 1
                devices = future.result()
                if devices:
                    responsive_count += 1
                    results.extend(devices)
            except Exception as e:
                app.logger.error(f'Ошибка при проверке IP: {e}')
    
    app.logger.info(f'Сканирование завершено: проверено {checked_count}/{len(ips)} IP, ответило {responsive_count}, найдено устройств: {len(results)}')
    
    return jsonify({
        'success': True,
        'range': range_input,
        'scanned_ips': len(ips),
        'found_devices': len(results),
        'results': results
    })


@app.route('/api/favorites/<string:device_type>/<int:device_id>', methods=['PUT', 'POST'])
def toggle_favorite(device_type, device_id):
    """Установка/переключение избранного для серверов и принтеров"""
    data = request.json or {}

    if device_type == 'server':
        device = Server.query.get(device_id)
    elif device_type == 'printer':
        device = Printer.query.get(device_id)
    elif device_type == 'camera':
        device = Camera.query.get(device_id)
    elif device_type == 'router':
        device = Router.query.get(device_id)
    else:
        return jsonify({'error': 'Неверный тип устройства'}), 400

    if not device:
        return jsonify({'error': 'Устройство не найдено'}), 404

    if 'is_favorite' in data:
        device.is_favorite = bool(data['is_favorite'])
    else:
        device.is_favorite = not bool(getattr(device, 'is_favorite', False))

    db.session.commit()
    return jsonify({'success': True, 'id': device_id, 'type': device_type, 'is_favorite': bool(device.is_favorite)})


@app.route('/api/admin/clear_db', methods=['POST', 'PUT'])
def clear_db():
    """Очистка данных из БД (таблицы остаются)"""
    data = request.json or {}
    if data.get('confirm') != 'CLEAR' or data.get('confirm2') != 'CLEAR':
        return jsonify({'error': 'Требуется подтверждение'}), 400

    try:
        clear_servers = bool(data.get('clear_servers', True))
        clear_printers = bool(data.get('clear_printers', True))
        clear_cameras = bool(data.get('clear_cameras', True))
        clear_routers = bool(data.get('clear_routers', True))
        clear_groups = bool(data.get('clear_groups', True))

        if clear_groups:
            # Если удаляем группы, сначала отвязываем устройства, чтобы не было проблем с FK.
            Server.query.update({'group_id': None}, synchronize_session=False)
            Printer.query.update({'group_id': None}, synchronize_session=False)
            Camera.query.update({'group_id': None}, synchronize_session=False)
            Router.query.update({'group_id': None}, synchronize_session=False)

        if clear_servers:
            Server.query.delete(synchronize_session=False)
        if clear_printers:
            Printer.query.delete(synchronize_session=False)
        if clear_cameras:
            Camera.query.delete(synchronize_session=False)
        if clear_routers:
            Router.query.delete(synchronize_session=False)
        if clear_groups:
            # У групп есть иерархия (parent_id). Перед удалением разрываем связи,
            # иначе SQLite может ругаться на FK при массовом delete.
            Group.query.update({'parent_id': None}, synchronize_session=False)
            Group.query.delete(synchronize_session=False)

        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': f'{type(e).__name__}: {str(e)}'}), 500


@app.route('/api/novnc/<int:server_id>/token', methods=['POST'])
def novnc_token(server_id):
    """
    Выдаёт временный токен для noVNC. Токен мапится на host:port в novnc_tokens.txt
    и используется websockify (TokenFile).
    """
    server = Server.query.get(server_id)
    if not server:
        return jsonify({'success': False, 'message': 'Сервер не найден'}), 404

    token = secrets.token_urlsafe(18)
    now = time.time()
    expires_at = now + NOVNC_TOKEN_TTL_SECONDS

    with _novnc_lock:
        _prune_novnc_tokens_locked(now)
        _novnc_tokens[token] = (server.ip, server.port, expires_at)
        _write_novnc_token_file_locked()

    return jsonify(
        {
            "success": True,
            "token": token,
            "proxy_port": NOVNC_PROXY_PORT,
            "expires_in_seconds": NOVNC_TOKEN_TTL_SECONDS,
            "server": {"id": server.id, "name": server.name, "ip": server.ip, "port": server.port},
        }
    )


@app.route('/novnc/<int:server_id>')
def novnc_page(server_id):
    server = Server.query.get(server_id)
    if not server:
        return "Сервер не найден", 404

    safe_name = escape(server.name)
    ws_path = NOVNC_WS_PATH.replace('\\', '/')
    if ws_path and not ws_path.startswith('/'):
        ws_path = '/' + ws_path

    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>noVNC — {safe_name}</title>
  <style>
    :root {{
      --bg: #0f172a;
      --panel: #111827;
      --text: #e5e7eb;
      --muted: #9ca3af;
      --ok: #10b981;
      --bad: #ef4444;
      --btn: #2563eb;
      --btn2: #374151;
      --border: rgba(255,255,255,.12);
    }}
    html, body {{ height: 100%; margin: 0; background: var(--bg); color: var(--text); font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; }}
    .wrap {{ height: 100%; }}
    button {{
      height: 34px; border-radius: 8px; border: 1px solid var(--border); cursor: pointer;
      padding: 0 10px; color: var(--text); background: var(--btn2);
    }}
    button.primary {{ background: var(--btn); border-color: rgba(37,99,235,.6); }}
    .status-badge {{
      position: fixed; top: 10px; right: 10px; z-index: 50;
      font-size: 11px; color: var(--text); background: var(--panel);
      border: 1px solid var(--border); border-radius: 6px; padding: 3px 8px;
      pointer-events: none; opacity: .92;
    }}
    .status-badge.ok {{ color: var(--ok); }}
    .status-badge.bad {{ color: var(--bad); }}
    #screen {{
      width: 100%;
      height: 100%;
      background: #000;
      display: flex;
      align-items: center;
      justify-content: center;
      overflow: hidden;
    }}
    #screen canvas {{
      /* Вписываем с сохранением пропорций (letterbox/pillarbox) */
      max-width: 100% !important;
      max-height: 100% !important;
      width: auto !important;
      height: auto !important;
    }}
    .pw-overlay {{
      position: fixed; inset: 0; background: rgba(0,0,0,.55);
      display: none; align-items: center; justify-content: center; z-index: 100;
    }}
    .pw-overlay.open {{ display: flex; }}
    .pw-box {{
      background: var(--panel); border: 1px solid var(--border); border-radius: 12px;
      padding: 20px 22px; width: 320px; max-width: calc(100vw - 40px);
    }}
    .pw-box h4 {{ margin: 0 0 6px; font-size: 15px; }}
    .pw-box p {{ margin: 0 0 14px; font-size: 12px; color: var(--muted); }}
    .pw-box input {{
      width: 100%; box-sizing: border-box; height: 36px; padding: 0 10px;
      border-radius: 8px; border: 1px solid var(--border); background: var(--bg);
      color: var(--text); font-size: 14px;
    }}
    .pw-btns {{ display: flex; gap: 8px; justify-content: flex-end; margin-top: 14px; }}

    #clipCapture {{
      position: fixed; left: 50%; bottom: 20px; transform: translateX(-50%);
      z-index: 300; width: 320px; height: 36px; padding: 6px 10px;
      border: 1px solid var(--border); border-radius: 8px;
      background: var(--panel); color: var(--text); font-size: 13px;
      opacity: 0; pointer-events: none; transition: opacity .15s;
    }}
    #clipCapture.open {{
      opacity: 1; pointer-events: auto;
    }}
    #clipToast {{
      position: fixed; left: 50%; bottom: 20px; transform: translateX(-50%);
      z-index: 310; max-width: 420px; padding: 7px 14px; border-radius: 8px;
      background: rgba(15, 23, 42, .9); border: 1px solid var(--border);
      color: var(--text); font-size: 12px; opacity: 0; pointer-events: none;
      transition: opacity .2s;
    }}
    #clipToast.show {{
      opacity: 1;
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <div id="screen"></div>
    <div id="status" class="status-badge">Подключение…</div>
  </div>

  <div class="pw-overlay" id="pwOverlay">
    <div class="pw-box">
      <h4>Требуется пароль VNC</h4>
      <p>Введите пароль для подключения к серверу. Пароль не сохраняется.</p>
      <input type="password" id="pwInput" autocomplete="off" spellcheck="false" />
      <div class="pw-btns">
        <button id="pwCancel">Отмена</button>
        <button class="primary" id="pwOk">Подключиться</button>
      </div>
    </div>
  </div>

  <textarea id="clipCapture" tabindex="-1" placeholder="Ctrl+V — вставить и отправить на удалённую машину"></textarea>
  <div id="clipToast"></div>

  <script type="module">
    import RFB from 'https://cdn.jsdelivr.net/gh/novnc/noVNC@v{NOVNC_CDN_VERSION}/core/rfb.js';

    const serverId = {server.id};
    const novncWsPath = {json.dumps(ws_path)};
    const statusEl = document.getElementById('status');
    const screen = document.getElementById('screen');

    let rfb = null;
    let ro = null;
    const pwOverlay = document.getElementById('pwOverlay');
    const pwInput = document.getElementById('pwInput');

    function openPwModal() {{
      pwInput.value = '';
      pwOverlay.classList.add('open');
      setTimeout(() => pwInput.focus(), 50);
    }}
    function closePwModal() {{
      pwOverlay.classList.remove('open');
    }}

    document.getElementById('pwOk').addEventListener('click', () => {{
      const password = pwInput.value;
      closePwModal();
      if (rfb) {{
        try {{ rfb.sendCredentials({{ password }}); }} catch (e) {{
          setStatus('Не удалось отправить пароль', 'bad');
        }}
      }}
    }});
    document.getElementById('pwCancel').addEventListener('click', closePwModal);
    pwInput.addEventListener('keydown', (e) => {{
      if (e.key === 'Enter') document.getElementById('pwOk').click();
      if (e.key === 'Escape') closePwModal();
    }});

    function setStatus(text, kind) {{
      statusEl.textContent = text;
      statusEl.classList.remove('ok', 'bad');
      if (kind) statusEl.classList.add(kind);
    }}

    async function connect() {{
      try {{
        setStatus('Получаю токен…');
        const headers = {{}};
        const apiKey = localStorage.getItem('vnc_api_key');
        if (apiKey) headers['X-API-Key'] = apiKey;
        const resp = await fetch(`/api/novnc/${{serverId}}/token`, {{ method: 'POST', headers }});
        const data = await resp.json();
        if (!resp.ok || !data.success) {{
          throw new Error(data.message || 'Не удалось получить токен');
        }}

        const scheme = (location.protocol === 'https:') ? 'wss' : 'ws';
        const host = location.hostname;
        const wsUrl = novncWsPath
          ? `${{scheme}}://${{host}}${{novncWsPath}}?token=${{encodeURIComponent(data.token)}}`
          : `${{scheme}}://${{host}}:${{data.proxy_port}}/?token=${{encodeURIComponent(data.token)}}`;

        if (rfb) {{
          try {{ rfb.disconnect(); }} catch (_) {{}}
          rfb = null;
        }}

        setStatus('Подключаюсь…');
        rfb = new RFB(screen, wsUrl, {{ shared: true }});
        // Масштабируем картинку под контейнер с сохранением пропорций
        rfb.scaleViewport = true;
        rfb.clipViewport = false;
        // Не навязываем удалённой машине новый размер: сохраняем её разрешение,
        // а в браузере делаем корректное (proportional) масштабирование.
        rfb.resizeSession = false;

        rfb.addEventListener('connect', () => setStatus('Подключено', 'ok'));
        rfb.addEventListener('disconnect', (e) => {{
          const detail = e?.detail;
          const clean = detail?.clean;
          const reason = detail?.reason || '';
          setStatus(clean ? 'Отключено' : `Ошибка: ${{reason || 'соединение потеряно'}}`, clean ? undefined : 'bad');
        }});

        rfb.addEventListener('credentialsrequired', () => {{
          openPwModal();
        }});

        // Двусторонний буфер обмена: приём текста с удалённой машины.
        // noVNC в legacy-режиме отдаёт "сырые" байты (каждый символ ≤ 255),
        // а в extended-режиме уже декодирует UTF-8 (символы > 255).
        // Кодировку legacy-байтов определяем: пробуем UTF-8, затем Windows-1251
        // (TightVNC/UltraVNC на русской Windows передают буфер в ANSI — CP1251).
        rfb.addEventListener('clipboard', (e) => {{
          let text = e?.detail?.text;
          if (!text) return;
          if (text.charCodeAt(text.length - 1) === 0) {{
            text = text.slice(0, -1);
          }}
          let isRawBytes = true;
          for (let i = 0; i < text.length; i++) {{
            if (text.charCodeAt(i) > 255) {{ isRawBytes = false; break; }}
          }}
          if (isRawBytes) {{
            text = decodeClipboardBytes(text);
          }}
          writeToLocalClipboard(text);
        }});

        // Автомасштабирование при изменении размеров вкладки/контейнера
        if (ro) {{
          try {{ ro.disconnect(); }} catch (_) {{}}
          ro = null;
        }}
        ro = new ResizeObserver(() => {{
          if (!rfb) return;
          // сеттер scaleViewport триггерит пересчёт масштаба
          rfb.scaleViewport = true;
        }});
        ro.observe(screen);
      }} catch (e) {{
        setStatus(`Ошибка: ${{e.message || e}}`, 'bad');
      }}
    }}

    window.addEventListener('resize', () => {{
      if (rfb) rfb.scaleViewport = true;
    }});

    // ---------- Буфер обмена ----------
    const clipCapture = document.getElementById('clipCapture');
    const clipToast = document.getElementById('clipToast');
    let clipToastTimer = null;

    // Кодировка legacy-буфера. По умолчанию CP1251: TightVNC на русской Windows
    // передаёт клиенту буфер в системной ANSI-кодировке. После первого приёма
    // автоматически обновляется по факту декодирования (utf8 или cp1251).
    let clipEncoding = 'cp1251';

    // Таблица Windows-1251 для байтов 0x80..0xFF (эталон — кодек Python cp1251)
    const CP1251_TABLE = [
      0x0402,0x0403,0x201A,0x0453,0x201E,0x2026,0x2020,0x2021,
      0x20AC,0x2030,0x0409,0x2039,0x040A,0x040C,0x040B,0x040F,
      0x0452,0x2018,0x2019,0x201C,0x201D,0x2022,0x2013,0x2014,
      0xFFFD,0x2122,0x0459,0x203A,0x045A,0x045C,0x045B,0x045F,
      0x00A0,0x040E,0x045E,0x0408,0x00A4,0x0490,0x00A6,0x00A7,
      0x0401,0x00A9,0x0404,0x00AB,0x00AC,0x00AD,0x00AE,0x0407,
      0x00B0,0x00B1,0x0406,0x0456,0x0491,0x00B5,0x00B6,0x00B7,
      0x0451,0x2116,0x0454,0x00BB,0x0458,0x0405,0x0455,0x0457,
      0x0410,0x0411,0x0412,0x0413,0x0414,0x0415,0x0416,0x0417,
      0x0418,0x0419,0x041A,0x041B,0x041C,0x041D,0x041E,0x041F,
      0x0420,0x0421,0x0422,0x0423,0x0424,0x0425,0x0426,0x0427,
      0x0428,0x0429,0x042A,0x042B,0x042C,0x042D,0x042E,0x042F,
      0x0430,0x0431,0x0432,0x0433,0x0434,0x0435,0x0436,0x0437,
      0x0438,0x0439,0x043A,0x043B,0x043C,0x043D,0x043E,0x043F,
      0x0440,0x0441,0x0442,0x0443,0x0444,0x0445,0x0446,0x0447,
      0x0448,0x0449,0x044A,0x044B,0x044C,0x044D,0x044E,0x044F
    ];
    const CP1251_REV = new Map();
    for (let i = 0; i < CP1251_TABLE.length; i++) CP1251_REV.set(CP1251_TABLE[i], 0x80 + i);

    function decodeCp1251(bytes) {{
      let out = '';
      for (let i = 0; i < bytes.length; i++) {{
        const b = bytes[i];
        out += String.fromCharCode(b < 0x80 ? b : CP1251_TABLE[b - 0x80]);
      }}
      return out;
    }}

    function decodeClipboardBytes(text) {{
      const bytes = new Uint8Array(text.length);
      for (let i = 0; i < text.length; i++) bytes[i] = text.charCodeAt(i);
      try {{
        const decoded = new TextDecoder('utf-8', {{ fatal: true }}).decode(bytes);
        clipEncoding = 'utf8';
        return decoded;
      }} catch (_) {{
        clipEncoding = 'cp1251';
        return decodeCp1251(bytes);
      }}
    }}

    function encodeCp1251String(str) {{
      let out = '';
      for (const ch of str) {{
        const cp = ch.codePointAt(0);
        if (cp < 0x80) {{
          out += ch;
        }} else {{
          const byte = CP1251_REV.get(cp);
          out += byte === undefined ? '?' : String.fromCharCode(byte);
        }}
      }}
      return out;
    }}

    function showClipToast(text) {{
      clipToast.textContent = text;
      clipToast.classList.add('show');
      clearTimeout(clipToastTimer);
      clipToastTimer = setTimeout(() => clipToast.classList.remove('show'), 2600);
    }}

    function writeToLocalClipboard(text) {{
      if (navigator.clipboard && navigator.clipboard.writeText) {{
        navigator.clipboard.writeText(text)
          .then(() => showClipToast('Скопировано с удалённой машины'))
          .catch(() => {{
            try {{ legacyCopy(text); }} catch (_) {{}}
          }});
      }} else {{
        try {{ legacyCopy(text); }} catch (_) {{}}
      }}
    }}

    function legacyCopy(text) {{
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.cssText = 'position:fixed; top:0; left:0; width:2px; height:2px; opacity:0;';
      document.body.appendChild(ta);
      ta.focus();
      ta.select();
      const ok = document.execCommand('copy');
      ta.remove();
      if (ok) showClipToast('Скопировано с удалённой машины');
      else showClipToast('Текст получен с удалённой машины');
    }}

    function sendToRemote(text) {{
      if (!text || !rfb) return;
      try {{
        // Extended clipboard (UTF-8) noVNC обрабатывает сам.
        // В legacy-режиме noVNC кодирует текст как Latin-1 и заменяет символы
        // > 0xff на '?' (кириллица ломается). Кодируем сами в соответствии с
        // кодировкой, определённой при приёме (CP1251 для TightVNC), и передаём
        // байты как "latin1"-строку — noVNC отправит их как есть.
        const extText = !!(rfb._clipboardServerCapabilitiesFormats &&
                           rfb._clipboardServerCapabilitiesFormats[1] &&
                           rfb._clipboardServerCapabilitiesActions &&
                           rfb._clipboardServerCapabilitiesActions[1 << 27]);
        if (extText) {{
          rfb.clipboardPasteFrom(text);
        }} else if (clipEncoding === 'cp1251') {{
          rfb.clipboardPasteFrom(encodeCp1251String(text));
        }} else {{
          const bytes = new TextEncoder().encode(text);
          let latin = '';
          for (let i = 0; i < bytes.length; i++) latin += String.fromCharCode(bytes[i]);
          rfb.clipboardPasteFrom(latin);
        }}
      }} catch (_) {{}}
      showClipToast('Отправлено на удалённую машину');
    }}

    // Отправка локального буфера на удалённую машину по Ctrl+V.
    // Перехват в фазе capture, чтобы срабатывать раньше обработчиков noVNC.
    window.addEventListener('keydown', (e) => {{
      const ae = document.activeElement;
      if (ae && (ae.tagName === 'INPUT' || ae.tagName === 'TEXTAREA')) return;
      if (!(e.ctrlKey || e.metaKey)) return;
      if (e.key.toLowerCase() !== 'v') return;
      if (navigator.clipboard && navigator.clipboard.readText) {{
        // HTTPS: читаем буфер напрямую (вызов происходит в рамках жеста пользователя)
        e.preventDefault();
        navigator.clipboard.readText().then(sendToRemote).catch(() => {{
          openClipCapture();
        }});
      }} else {{
        // HTTP: перенаправляем вставку в скрытое поле и забираем текст оттуда
        e.preventDefault();
        openClipCapture();
      }}
    }}, true);

    function openClipCapture() {{
      clipCapture.value = '';
      clipCapture.classList.add('open');
      clipCapture.focus();
      clipCapture._handled = false;
      const onInput = () => {{
        if (clipCapture._handled) return;
        clipCapture._handled = true;
        const text = clipCapture.value;
        clipCapture.classList.remove('open');
        sendToRemote(text);
      }};
      clipCapture.addEventListener('input', onInput);
      clipCapture.addEventListener('paste', onInput);
    }}

    // Автоподключение
    connect();
  </script>
</body>
</html>"""

@app.route('/rustdesk/<int:server_id>')
def rustdesk_page(server_id):
    server = Server.query.get(server_id)
    if not server:
        return "Сервер не найден", 404

    safe_name = escape(server.name)
    rustdesk_id = (server.rustdesk_id or '').strip()
    rd_server = ( _get_setting('rustdesk_server', '') or '').strip()
    rd_key = ( _get_setting('rustdesk_key', '') or '').strip()

    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>RustDesk — {safe_name}</title>
  <style>
    :root {{
      --bg: #0f172a; --panel: #111827; --text: #e5e7eb; --muted: #9ca3af;
      --ok: #10b981; --bad: #ef4444; --btn: #2563eb; --btn2: #374151;
      --border: rgba(255,255,255,.12);
    }}
    html, body {{ height: 100%; margin: 0; background: var(--bg); color: var(--text); font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; }}
    button {{
      height: 34px; border-radius: 8px; border: 1px solid var(--border); cursor: pointer;
      padding: 0 12px; color: var(--text); background: var(--btn2); font-size: 14px;
    }}
    button.primary {{ background: var(--btn); border-color: rgba(37,99,235,.6); }}
    .modal-backdrop {{
      position: fixed; inset: 0; background: rgba(0,0,0,.55); z-index: 100;
      display: flex; align-items: center; justify-content: center;
    }}
    .modal-box {{
      background: var(--panel); border: 1px solid var(--border); border-radius: 12px;
      padding: 22px; width: 360px; max-width: calc(100vw - 40px);
    }}
    .modal-box h3 {{ margin: 0 0 6px; font-size: 16px; }}
    .modal-box p {{ margin: 0 0 14px; font-size: 12px; color: var(--muted); }}
    .modal-box label {{ display: block; font-size: 12px; margin-bottom: 6px; }}
    .modal-box input {{
      width: 100%; box-sizing: border-box; height: 36px; padding: 0 10px;
      border-radius: 8px; border: 1px solid var(--border); background: var(--bg);
      color: var(--text); font-size: 14px; margin-bottom: 12px;
    }}
    .modal-box .btns {{ display: flex; gap: 8px; justify-content: flex-end; }}
    .err {{ color: var(--bad); font-size: 12px; margin-bottom: 10px; display: none; }}
    .hint {{ font-size: 11px; color: var(--muted); margin-top: 10px; line-height: 1.5; }}
  </style>
</head>
<body>
  <div class="modal-backdrop" id="setupModal">
    <div class="modal-box">
      <h3>RustDesk — {safe_name}</h3>
      <p>Укажите параметры подключения. Пароль запрашивается каждый раз и не сохраняется.</p>
      <div class="err" id="errMsg"></div>
      <label>Сервер (hbbs/hbbr)</label>
      <input type="text" id="rdServer" placeholder="192.168.17.250" />
      <label>Ключ сервера (публичный)</label>
      <input type="text" id="rdKey" placeholder="Публичный ключ сервера" />
      <label>ID машины RustDesk</label>
      <input type="text" id="rdId" placeholder="123 456 789" />
      <div class="btns">
        <button id="btnCancel">Отмена</button>
        <button class="primary" id="btnLaunch">Запустить</button>
      </div>
      <div class="hint">Браузер выступает управляющей стороной. На удалённой машине должен быть установлен и запущен RustDesk с тем же сервером.</div>
    </div>
  </div>
  <iframe id="rdFrame" src="/rustdesk_web/index.html" style="position:fixed; top:0; left:0; width:100vw; height:100vh; border:none; display:none;"></iframe>

  <script>
    const serverId = {server.id};
    const initialServer = {json.dumps(rd_server)};
    const initialKey = {json.dumps(rd_key)};
    const initialId = {json.dumps(rustdesk_id)};

    document.getElementById('rdServer').value = initialServer;
    document.getElementById('rdKey').value = initialKey;
    document.getElementById('rdId').value = initialId;

    function showErr(msg) {{
      const el = document.getElementById('errMsg');
      el.textContent = msg;
      el.style.display = 'block';
    }}

    document.getElementById('btnLaunch').addEventListener('click', () => {{
      const server = document.getElementById('rdServer').value.trim();
      const id = document.getElementById('rdId').value.trim();
      if (!server) return showErr('Укажите адрес сервера');
      if (!id) return showErr('Укажите ID машины');
      startRd();
    }});

    document.getElementById('btnCancel').addEventListener('click', () => window.close());

    function startRd() {{
      const server = document.getElementById('rdServer').value.trim();
      const key = document.getElementById('rdKey').value.trim();
      const id = document.getElementById('rdId').value.trim();

      localStorage.setItem('wc-custom-rendezvous-server', server);
      localStorage.setItem('wc-key', key);
      localStorage.setItem('wc-id', id);

      document.getElementById('setupModal').style.display = 'none';
      const frame = document.getElementById('rdFrame');
      frame.style.display = 'block';
      frame.onload = function () {{ autoConnectRd(); }};
      frame.src = '/rustdesk_web/index.html?_rd=' + Date.now();
    }}

    // Дождаться готовности веб-клиента и запустить подключение к ID напрямую
    function autoConnectRd() {{
      const frame = document.getElementById('rdFrame');
      const server = document.getElementById('rdServer').value.trim();
      const key = document.getElementById('rdKey').value.trim();
      const id = document.getElementById('rdId').value.trim();
      if (!id) return;
      let attempts = 0;
      const maxAttempts = 10;
      function tryConnect() {{
        attempts++;
        let w = null;
        try {{ w = frame.contentWindow; }} catch (e) {{}}
        if (!w || typeof w.setByName !== 'function') {{
          if (attempts <= 120) setTimeout(tryConnect, 500);
          return;
        }}
        try {{
          w.localStorage.setItem('wc-custom-rendezvous-server', server);
          w.localStorage.setItem('wc-key', key);
          w.setByName('session_add_sync', JSON.stringify({{ id: id }}));
          const c = w.curConn;
          if (c) {{
            // start() блокируется лицензионной проверкой (gn -> libsodium), пока
            // не инициализирован sodium; _start() начинает подключение напрямую
            const fn = (typeof c._start === 'function') ? c._start : c.start;
            const p = fn.call(c);
            if (p && typeof p.then === 'function') {{
              p.catch(function (e) {{
                console.error('RustDesk connect error', e);
                // клиент может быть ещё не готов — повторяем
                if (attempts < maxAttempts) setTimeout(tryConnect, 1500);
              }});
            }}
          }}
        }} catch (e) {{
          console.error('RustDesk auto-connect error', e);
          if (attempts < maxAttempts) setTimeout(tryConnect, 1500);
        }}
      }}
      tryConnect();
    }}

    // Автостарт: параметры уже прописаны — подключаемся сразу
    if (initialServer && initialId) {{
      startRd();
    }}
  </script>
</body>
</html>"""


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


@app.route('/api/rustdesk/devices', methods=['GET'])
def rustdesk_devices():
    """Список устройств из панели RustDesk API (lejianwen/rustdesk-api)."""
    api_url = (_get_setting('rustdesk_api_url', '') or '').strip()
    username = (_get_setting('rustdesk_api_user', '') or '').strip()
    password = _get_setting('rustdesk_api_pass', '')
    if not api_url or not username or not password:
        return jsonify({'success': False,
                        'error': 'Настройки RustDesk API не заполнены (адрес панели, логин, пароль).'}), 400

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
            return jsonify({'success': False, 'error': err}), 502
        with _rd_token_lock:
            _rd_token_cache[api_url] = token

    try:
        r = get_peer_list(token)
    except requests.RequestException as e:
        with _rd_token_lock:
            _rd_token_cache.pop(api_url, None)
        return jsonify({'success': False, 'error': 'Ошибка запроса к RustDesk API: %s' % e}), 502

    if r.status_code == 403:
        # Токен протух — перелогиниваемся и пробуем ещё раз
        with _rd_token_lock:
            token, err = _rustdesk_login(api_url, username, password)
        if err:
            return jsonify({'success': False, 'error': err}), 502
        with _rd_token_lock:
            _rd_token_cache[api_url] = token
        try:
            r = get_peer_list(token)
        except requests.RequestException as e:
            return jsonify({'success': False, 'error': 'Ошибка запроса к RustDesk API: %s' % e}), 502

    if r.status_code != 200:
        return jsonify({'success': False, 'error': 'RustDesk API вернул HTTP %s' % r.status_code}), 502
    try:
        data = r.json()
    except ValueError:
        return jsonify({'success': False, 'error': 'Некорректный ответ RustDesk API'}), 502

    payload = data.get('data') or {}
    peers = payload.get('list') or []
    now = time.time()

    # Карты привязок и совпадений по IP для кнопки «Добавить»
    linked_ids = set()
    ip_server_map = {}
    for s in Server.query.all():
        if s.rustdesk_id:
            linked_ids.add(s.rustdesk_id)
        if s.ip and s.ip not in ip_server_map:
            ip_server_map[s.ip] = s

    devices = []
    for p in peers:
        last_ts = p.get('last_online_time') or 0
        online = bool(last_ts) and (now - last_ts) < _RD_ONLINE_WINDOW
        rd_id = p.get('id') or ''
        rd_ip = p.get('last_online_ip') or ''
        matched = ip_server_map.get(rd_ip)
        devices.append({
            'id': rd_id,
            'hostname': p.get('hostname') or '',
            'os': p.get('os') or '',
            'username': p.get('username') or '',
            'alias': p.get('alias') or '',
            'last_online_time': last_ts,
            'last_online_ip': rd_ip,
            'online': online,
            'linked': bool(rd_id) and rd_id in linked_ids,
            'matched_server': ({'id': matched.id, 'name': matched.name} if matched else None),
        })
    return jsonify({
        'success': True,
        'devices': devices,
        'total': payload.get('total', len(devices)),
    })

@app.route('/api/rustdesk/add', methods=['POST'])
def rustdesk_add():
    """Добавить устройство RustDesk в серверы.
    Если сервер с таким IP уже есть — прописать к нему RustDesk ID,
    иначе создать новый сервер."""
    data = get_json()
    rd_id = (data.get('id') or '').strip()
    ip = (data.get('ip') or '').strip()

    if not rd_id:
        return jsonify({'success': False, 'error': 'Не указан RustDesk ID устройства'}), 400
    if not ip:
        return jsonify({'success': False, 'error': 'У устройства нет IP-адреса'}), 400

    existing = Server.query.filter_by(ip=ip).first()
    if existing:
        # Прописываем RustDesk ID к уже существующему серверу
        for other in Server.query.filter(Server.rustdesk_id == rd_id, Server.id != existing.id).all():
            other.rustdesk_id = ''
        existing.rustdesk_id = rd_id
        db.session.commit()
        return jsonify({
            'success': True,
            'action': 'linked',
            'server_id': existing.id,
            'server_name': existing.name,
        })

    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'success': False, 'error': 'Введите название сервера'}), 400

    port = _normalize_port(data.get('port', 5900))
    if port is None:
        return jsonify({'success': False, 'error': 'Порт должен быть числом от 1 до 65535'}), 400

    group_id = data.get('group_id') or None
    server = Server(
        name=name,
        ip=ip,
        port=port,
        group_id=group_id,
        comment=(data.get('comment') or '').strip(),
        rustdesk_id=rd_id,
    )
    try:
        db.session.add(server)
        db.session.commit()
    except Exception:
        db.session.rollback()
        # Конкурентное создание сервера с тем же IP — привязываем к существующему
        dup = Server.query.filter_by(ip=ip).first()
        if dup:
            for other in Server.query.filter(Server.rustdesk_id == rd_id, Server.id != dup.id).all():
                other.rustdesk_id = ''
            dup.rustdesk_id = rd_id
            db.session.commit()
            return jsonify({
                'success': True,
                'action': 'linked',
                'server_id': dup.id,
                'server_name': dup.name,
            })
        return jsonify({'success': False, 'error': 'Ошибка сохранения сервера'}), 500

    return jsonify({
        'success': True,
        'action': 'created',
        'server_id': server.id,
        'server_name': server.name,
    })

@app.route('/api/export', methods=['GET'])
def export_data():
    """Экспорт всех данных в JSON"""
    try:
        groups = Group.query.all()
        servers = Server.query.all()
        printers = Printer.query.all()
        cameras = Camera.query.all()
        routers = Router.query.all()

        server_statuses = _fetch_statuses_parallel(servers)
        printer_statuses = _fetch_statuses_parallel(printers, is_printer=True)
        camera_statuses = _fetch_statuses_parallel(cameras, is_web=True)
        router_statuses = _fetch_statuses_parallel(routers, is_web=True)

        groups_data = []
        for group in groups:
            groups_data.append({
                'id': group.id,
                'name': group.name,
                'color': group.color,
                'parent_id': group.parent_id
            })
        
        servers_data = []
        for server in servers:
            servers_data.append({
                'id': server.id,
                'name': server.name,
                'ip': server.ip,
                'port': server.port,
                'group_id': server.group_id,
                'is_favorite': server.is_favorite,
                'last_seen': server.last_seen.isoformat() if server.last_seen else None,
                'comment': server.comment,
                'created_at': server.created_at.isoformat() if server.created_at else None,
                'status': 'online' if server_statuses.get(server.id) else 'offline'
            })
        
        printers_data = []
        for printer in printers:
            printers_data.append({
                'id': printer.id,
                'name': printer.name,
                'ip': printer.ip,
                'group_id': printer.group_id,
                'web_interface': printer.web_interface,
                'is_favorite': bool(getattr(printer, 'is_favorite', False)),
                'status': 'online' if printer_statuses.get(printer.id) else 'offline',
                'comment': printer.comment,
                'created_at': printer.created_at.isoformat() if printer.created_at else None
            })
        
        cameras_data = []
        for camera in cameras:
            cameras_data.append({
                'id': camera.id,
                'name': camera.name,
                'ip': camera.ip,
                'port': camera.port,
                'group_id': camera.group_id,
                'web_interface': camera.web_interface,
                'rtsp_url': camera.rtsp_url,
                'username': camera.username,
                'password': camera.password or '',
                'is_favorite': bool(getattr(camera, 'is_favorite', False)),
                'status': 'online' if camera_statuses.get(camera.id) else 'offline',
                'comment': camera.comment,
                'created_at': camera.created_at.isoformat() if camera.created_at else None
            })

        routers_data = []
        for router in routers:
            routers_data.append({
                'id': router.id,
                'name': router.name,
                'ip': router.ip,
                'port': router.port,
                'group_id': router.group_id,
                'web_interface': router.web_interface,
                'username': router.username,
                'password': router.password or '',
                'is_favorite': bool(getattr(router, 'is_favorite', False)),
                'status': 'online' if router_statuses.get(router.id) else 'offline',
                'comment': router.comment,
                'created_at': router.created_at.isoformat() if router.created_at else None
            })

        subnet_names_data = []
        for sn in SubnetName.query.all():
            subnet_names_data.append({'subnet': sn.subnet, 'name': sn.name})

        settings_data = {}
        for s in Settings.query.all():
            settings_data[s.key] = s.value

        export_data = {
            'metadata': {
                'exported_at': datetime.now().isoformat(),
                'version': '2.1',
                'total_servers': len(servers_data),
                'total_printers': len(printers_data),
                'total_cameras': len(cameras_data),
                'total_routers': len(routers_data),
                'total_groups': len(groups_data),
                'total_subnets': len(subnet_names_data),
                'total_settings': len(settings_data)
            },
            'groups': groups_data,
            'servers': servers_data,
            'printers': printers_data,
            'cameras': cameras_data,
            'routers': routers_data,
            'subnet_names': subnet_names_data,
            'settings': settings_data
        }
        
        return jsonify(export_data)
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _backup_to_import_payload(backup_data):
    """Преобразует формат файла бэкапа в формат импорта."""
    if backup_data.get('servers') or backup_data.get('printers') or backup_data.get('cameras') or backup_data.get('routers'):
        return {
            'mode': 'overwrite',
            'groups': backup_data.get('groups', []),
            'servers': backup_data.get('servers', []),
            'printers': backup_data.get('printers', []),
            'cameras': backup_data.get('cameras', []),
            'routers': backup_data.get('routers', []),
            'subnet_names': backup_data.get('subnetNames', []),
            'settings': backup_data.get('settings', {}),
        }

    servers = []
    printers = []
    cameras = []
    routers = []
    for device in backup_data.get('devices', []):
        if device.get('type') == 'server':
            servers.append({
                'name': device.get('name'),
                'ip': device.get('ip'),
                'port': device.get('port', 5900),
                'group_id': device.get('group_id'),
                'is_favorite': device.get('is_favorite', False),
                'comment': device.get('comment', ''),
                'rustdesk_id': device.get('rustdesk_id', ''),
            })
        elif device.get('type') == 'printer':
            printers.append({
                'name': device.get('name'),
                'ip': device.get('ip'),
                'group_id': device.get('group_id'),
                'web_interface': device.get('web_interface', f"http://{device.get('ip', '')}"),
                'is_favorite': device.get('is_favorite', False),
                'comment': device.get('comment', ''),
            })
        elif device.get('type') == 'camera':
            cameras.append({
                'name': device.get('name'),
                'ip': device.get('ip'),
                'port': device.get('port', 80),
                'group_id': device.get('group_id'),
                'web_interface': device.get('web_interface', f"http://{device.get('ip', '')}"),
                'rtsp_url': device.get('rtsp_url', ''),
                'username': device.get('username', ''),
                'password': device.get('password', ''),
                'is_favorite': device.get('is_favorite', False),
                'comment': device.get('comment', ''),
            })
        elif device.get('type') == 'router':
            routers.append({
                'name': device.get('name'),
                'ip': device.get('ip'),
                'port': device.get('port', 80),
                'group_id': device.get('group_id'),
                'web_interface': device.get('web_interface', f"http://{device.get('ip', '')}"),
                'username': device.get('username', ''),
                'password': device.get('password', ''),
                'is_favorite': device.get('is_favorite', False),
                'comment': device.get('comment', ''),
            })

    return {
        'mode': 'overwrite',
        'groups': backup_data.get('groups', []),
        'servers': servers,
        'printers': printers,
        'cameras': cameras,
        'routers': routers,
        'subnet_names': backup_data.get('subnetNames', []),
        'settings': backup_data.get('settings', {}),
    }


def import_data_core(data):
    if not data:
        raise ValueError('Нет данных для импорта')

    mode = data.get('mode', 'merge')

    if mode == 'overwrite':
        Server.query.update({'group_id': None}, synchronize_session=False)
        Printer.query.update({'group_id': None}, synchronize_session=False)
        Camera.query.update({'group_id': None}, synchronize_session=False)
        Router.query.update({'group_id': None}, synchronize_session=False)
        Group.query.update({'parent_id': None}, synchronize_session=False)
        Server.query.delete(synchronize_session=False)
        Printer.query.delete(synchronize_session=False)
        Camera.query.delete(synchronize_session=False)
        Router.query.delete(synchronize_session=False)
        Group.query.delete(synchronize_session=False)
        SubnetName.query.delete(synchronize_session=False)
        Settings.query.delete(synchronize_session=False)
        db.session.commit()

    groups_data = data.get('groups', [])
    group_map = {}

    for group_data in groups_data:
        if not group_data.get('name'):
            continue
        group_id = group_data.get('id')
        if group_id and Group.query.get(group_id):
            group = Group.query.get(group_id)
            group.name = group_data['name']
            group.color = group_data.get('color', '#3498db')
            group.parent_id = group_data.get('parent_id')
            new_group_id = group.id
        else:
            group = Group(
                name=group_data['name'],
                color=group_data.get('color', '#3498db'),
                parent_id=group_data.get('parent_id')
            )
            db.session.add(group)
            db.session.flush()
            new_group_id = group.id

        if group_id:
            group_map[group_id] = new_group_id

    db.session.commit()

    servers_data = data.get('servers', [])
    for server_data in servers_data:
        if not server_data.get('ip') or not server_data.get('name'):
            continue
        group_id = group_map.get(server_data.get('group_id')) if server_data.get('group_id') else None

        existing = Server.query.filter_by(ip=server_data['ip']).first()
        if existing:
            existing.name = server_data['name']
            existing.port = server_data.get('port', 5900)
            existing.group_id = group_id
            existing.is_favorite = server_data.get('is_favorite', False)
            existing.comment = server_data.get('comment', '')
            if 'rustdesk_id' in server_data:
                existing.rustdesk_id = (server_data.get('rustdesk_id') or '').strip()
        else:
            db.session.add(Server(
                name=server_data['name'],
                ip=server_data['ip'],
                port=server_data.get('port', 5900),
                group_id=group_id,
                is_favorite=server_data.get('is_favorite', False),
                comment=server_data.get('comment', ''),
                rustdesk_id=(server_data.get('rustdesk_id') or '').strip(),
            ))

    printers_data = data.get('printers', [])
    for printer_data in printers_data:
        if not printer_data.get('ip') or not printer_data.get('name'):
            continue
        group_id = group_map.get(printer_data.get('group_id')) if printer_data.get('group_id') else None

        existing = Printer.query.filter_by(ip=printer_data['ip']).first()
        if existing:
            existing.name = printer_data['name']
            existing.group_id = group_id
            existing.web_interface = printer_data.get('web_interface', f"http://{printer_data['ip']}")
            existing.is_favorite = printer_data.get('is_favorite', False)
            existing.comment = printer_data.get('comment', '')
        else:
            db.session.add(Printer(
                name=printer_data['name'],
                ip=printer_data['ip'],
                group_id=group_id,
                web_interface=printer_data.get('web_interface', f"http://{printer_data['ip']}"),
                is_favorite=printer_data.get('is_favorite', False),
                comment=printer_data.get('comment', ''),
            ))

    cameras_data = data.get('cameras', [])
    for camera_data in cameras_data:
        if not camera_data.get('ip') or not camera_data.get('name'):
            continue
        group_id = group_map.get(camera_data.get('group_id')) if camera_data.get('group_id') else None

        existing = Camera.query.filter_by(ip=camera_data['ip']).first()
        if existing:
            existing.name = camera_data['name']
            existing.port = camera_data.get('port', 80)
            existing.group_id = group_id
            existing.web_interface = camera_data.get('web_interface', f"http://{camera_data['ip']}")
            existing.rtsp_url = camera_data.get('rtsp_url', '')
            if 'username' in camera_data:
                existing.username = camera_data.get('username') or ''
            _apply_password(existing, camera_data)
            existing.is_favorite = camera_data.get('is_favorite', False)
            existing.comment = camera_data.get('comment', '')
        else:
            db.session.add(Camera(
                name=camera_data['name'],
                ip=camera_data['ip'],
                port=camera_data.get('port', 80),
                group_id=group_id,
                web_interface=camera_data.get('web_interface', f"http://{camera_data['ip']}"),
                rtsp_url=camera_data.get('rtsp_url', ''),
                username=camera_data.get('username', ''),
                password=camera_data.get('password', ''),
                is_favorite=camera_data.get('is_favorite', False),
                comment=camera_data.get('comment', ''),
            ))

    routers_data = data.get('routers', [])
    for router_data in routers_data:
        if not router_data.get('ip') or not router_data.get('name'):
            continue
        group_id = group_map.get(router_data.get('group_id')) if router_data.get('group_id') else None

        existing = Router.query.filter_by(ip=router_data['ip']).first()
        if existing:
            existing.name = router_data['name']
            existing.port = router_data.get('port', 80)
            existing.group_id = group_id
            existing.web_interface = router_data.get('web_interface', f"http://{router_data['ip']}")
            if 'username' in router_data:
                existing.username = router_data.get('username') or ''
            _apply_password(existing, router_data)
            existing.is_favorite = router_data.get('is_favorite', False)
            existing.comment = router_data.get('comment', '')
        else:
            db.session.add(Router(
                name=router_data['name'],
                ip=router_data['ip'],
                port=router_data.get('port', 80),
                group_id=group_id,
                web_interface=router_data.get('web_interface', f"http://{router_data['ip']}"),
                username=router_data.get('username', ''),
                password=router_data.get('password', ''),
                is_favorite=router_data.get('is_favorite', False),
                comment=router_data.get('comment', ''),
            ))

    subnet_names = data.get('subnet_names', [])
    for entry in subnet_names:
        if isinstance(entry, (list, tuple)) and len(entry) >= 2:
            subnet, name = entry[0], entry[1]
        elif isinstance(entry, dict):
            subnet, name = entry.get('subnet'), entry.get('name')
        else:
            continue
        if not subnet:
            continue
        record = SubnetName.query.filter_by(subnet=subnet).first()
        if name:
            if record:
                record.name = name
            else:
                db.session.add(SubnetName(subnet=subnet, name=name))
        elif record:
            db.session.delete(record)

    settings_data = data.get('settings') or {}
    if isinstance(settings_data, dict):
        for key, value in settings_data.items():
            if value is None or value == '':
                continue
            setting = Settings.query.filter_by(key=key).first()
            if setting:
                setting.value = str(value)
                setting.updated_at = utcnow()
            else:
                db.session.add(Settings(key=key, value=str(value), type='string', description=f'Настройка {key}'))

    db.session.commit()


@app.route('/api/import', methods=['POST'])
def import_data():
    """Импорт данных из JSON"""
    try:
        data = get_json()
        import_data_core(data)
        return jsonify({'success': True, 'message': 'Данные успешно импортированы'})
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

# API для настроек
def _get_setting(key, default=None):
    setting = Settings.query.filter_by(key=key).first()
    if setting and setting.value:
        return setting.value
    return default


@app.route('/api/settings', methods=['GET'])
def get_settings():
    """Получить все настройки"""
    settings = Settings.query.all()
    result = {}
    for setting in settings:
        # Секреты не отдаём клиенту в открытом виде
        if setting.key == 'rustdesk_api_pass':
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

@app.route('/api/settings', methods=['POST'])
def update_settings():
    """Обновить настройки"""
    try:
        data = get_json()
        
        for key, value in data.items():
            # Пустой пароль не перезаписываем (клиент не получает текущее значение)
            if key == 'rustdesk_api_pass' and not str(value):
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

    static_dir = os.path.join(app.static_folder, 'uploads')
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


@app.route('/api/settings/upload_favicon', methods=['POST'])
def upload_favicon():
    """Загрузить favicon"""
    try:
        return _save_uploaded_image('favicon', 'favicon_path', 'favicon')
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


@app.route('/api/settings/upload_logo', methods=['POST'])
def upload_logo():
    """Загрузить логотип"""
    try:
        return _save_uploaded_image('logo', 'logo_path', 'logo')
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

# API для названий подсетей
@app.route('/api/subnet_names', methods=['GET'])
def get_subnet_names():
    try:
        subnet_names = SubnetName.query.all()
        return jsonify([{'subnet': sn.subnet, 'name': sn.name} for sn in subnet_names])
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/subnet_names', methods=['POST'])
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

@app.route('/api/subnet_names/<subnet>', methods=['DELETE'])
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

# API для бэкапов и восстановления

@app.route('/api/create_backup', methods=['POST'])
def create_backup():
    try:
        data = request.get_json() or {}

        groups = [{
            'id': g.id, 'name': g.name, 'color': g.color, 'parent_id': g.parent_id,
        } for g in Group.query.all()]
        servers = [{
            'id': s.id, 'name': s.name, 'ip': s.ip, 'port': s.port,
            'group_id': s.group_id, 'is_favorite': s.is_favorite,
            'comment': s.comment, 'rustdesk_id': s.rustdesk_id or '',
        } for s in Server.query.all()]
        printers = [{
            'id': p.id, 'name': p.name, 'ip': p.ip, 'group_id': p.group_id,
            'web_interface': p.web_interface, 'is_favorite': p.is_favorite,
            'comment': p.comment,
        } for p in Printer.query.all()]
        cameras = [{
            'id': c.id, 'name': c.name, 'ip': c.ip, 'port': c.port,
            'group_id': c.group_id, 'web_interface': c.web_interface,
            'rtsp_url': c.rtsp_url, 'username': c.username, 'password': c.password or '',
            'is_favorite': c.is_favorite, 'comment': c.comment,
        } for c in Camera.query.all()]
        routers = [{
            'id': r.id, 'name': r.name, 'ip': r.ip, 'port': r.port,
            'group_id': r.group_id, 'web_interface': r.web_interface,
            'username': r.username, 'password': r.password or '',
            'is_favorite': r.is_favorite, 'comment': r.comment,
        } for r in Router.query.all()]
        subnet_names = [{'subnet': sn.subnet, 'name': sn.name} for sn in SubnetName.query.all()]
        settings = {s.key: s.value for s in Settings.query.all()}

        backup_data = {
            'timestamp': datetime.now().isoformat(),
            'comment': data.get('comment', ''),
            'appTitle': data.get('appTitle') or _get_setting('app_title', 'VNC Manager'),
            'theme': data.get('theme', ''),
            'groups': groups,
            'servers': servers,
            'printers': printers,
            'cameras': cameras,
            'routers': routers,
            'subnetNames': subnet_names,
            'settings': settings,
        }

        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"backup_{timestamp}.json"
        backup_path = os.path.join(BACKUPS_DIR, filename)

        with open(backup_path, 'w', encoding='utf-8') as f:
            json.dump(backup_data, f, ensure_ascii=False, indent=2)

        return jsonify({
            'success': True,
            'filename': filename,
            'message': f'Backup создан: {filename}'
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/backups', methods=['GET'])
def list_backups():
    try:
        if not os.path.exists(BACKUPS_DIR):
            return jsonify({'success': True, 'backups': []})

        backups = []
        for filename in os.listdir(BACKUPS_DIR):
            if not BACKUP_FILENAME_RE.fullmatch(filename):
                continue
            filepath = os.path.join(BACKUPS_DIR, filename)
            stat = os.stat(filepath)

            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    backup_data = json.load(f)
                    app_title = backup_data.get('appTitle', 'VNC Manager')
                    timestamp = backup_data.get('timestamp', '')
                    comment = backup_data.get('comment', '')
            except (OSError, json.JSONDecodeError):
                app_title = 'VNC Manager'
                timestamp = ''
                comment = ''

            backups.append({
                'filename': filename,
                'timestamp': timestamp,
                'appTitle': app_title,
                'comment': comment,
                'size': stat.st_size,
                'created': datetime.fromtimestamp(stat.st_ctime).isoformat()
            })

        backups.sort(key=lambda x: x['created'], reverse=True)
        return jsonify({'success': True, 'backups': backups})

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/restore_backup/<filename>', methods=['POST'])
def restore_backup(filename):
    try:
        backup_path = _safe_backup_path(filename)
        if not backup_path or not os.path.exists(backup_path):
            return jsonify({'error': 'Файл бэкапа не найден'}), 404

        with open(backup_path, 'r', encoding='utf-8') as f:
            backup_data = json.load(f)

        import_data_core(_backup_to_import_payload(backup_data))

        return jsonify({
            'success': True,
            'message': f'Данные восстановлены из {filename}'
        })

    except ValueError as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


@app.route('/api/delete_backups', methods=['POST'])
def delete_backups():
    try:
        data = request.get_json() or {}
        filenames = data.get('filenames', [])

        if not filenames:
            return jsonify({'error': 'Не указаны файлы для удаления'}), 400

        deleted_count = 0
        errors = []

        for filename in filenames:
            backup_path = _safe_backup_path(filename)
            try:
                if backup_path and os.path.exists(backup_path):
                    os.remove(backup_path)
                    deleted_count += 1
                else:
                    errors.append(f'Файл {filename} не найден')
            except OSError as e:
                errors.append(f'Ошибка удаления {filename}: {str(e)}')

        return jsonify({
            'success': True,
            'deleted_count': deleted_count,
            'errors': errors
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500

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
