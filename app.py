from flask import Flask, request, jsonify
import subprocess
import os
import socket
import secrets
import threading
import time
from datetime import datetime
import platform
from pathlib import Path
import ipaddress
import concurrent.futures
from models import db, Group, Server, Printer, Settings, SubnetName
from sqlalchemy import text

app = Flask(__name__, static_folder='.', static_url_path='')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///vnc_manager.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)

APP_BUILD_ID = f"{int(time.time())}:{Path(__file__).name}"


@app.route('/api/debug/routes', methods=['GET'])
def debug_routes():
    routes = []
    for rule in app.url_map.iter_rules():
        if rule.rule in (
            '/api/favorites/<string:device_type>/<int:device_id>',
            '/api/admin/clear_db',
            '/api/<path:any_path>',
        ):
            routes.append({'rule': rule.rule, 'methods': sorted(list(rule.methods or []))})
    return jsonify({'build_id': APP_BUILD_ID, 'routes': routes})


@app.after_request
def add_api_cors_headers(response):
    if request.path.startswith('/api/'):
        response.headers.setdefault('Access-Control-Allow-Origin', '*')
        response.headers.setdefault('Access-Control-Allow-Headers', 'Content-Type')
        response.headers.setdefault('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS')
    return response


@app.route('/api/<path:any_path>', methods=['OPTIONS'])
def api_options(any_path):
    return ('', 204)


@app.before_request
def _log_problem_endpoints():
    if request.path.startswith('/api/favorites/') or request.path == '/api/admin/clear_db':
        try:
            app.logger.info('API request: %s %s', request.method, request.path)
        except Exception:
            pass


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
NOVNC_PROXY_PORT = int(os.environ.get("NOVNC_PROXY_PORT", "6080"))
NOVNC_TOKEN_TTL_SECONDS = int(os.environ.get("NOVNC_TOKEN_TTL_SECONDS", "600"))
os.makedirs(app.instance_path, exist_ok=True)
_NOVNC_TOKEN_FILE = os.path.join(app.instance_path, "novnc_tokens.txt")

# Гарантируем, что файл токенов существует (даже пустой) — он нужен отдельному сервису websockify.
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


def init_db():
    """Инициализация базы данных с тестовыми данными"""
    with app.app_context():
        db.create_all()

        # Миграция: добавляем колонку is_favorite в таблицу printer, если её нет (для существующих БД)
        try:
            cols = [r[1] for r in db.session.execute(text("PRAGMA table_info(printer)"))]
            if 'is_favorite' not in cols:
                db.session.execute(text("ALTER TABLE printer ADD COLUMN is_favorite BOOLEAN DEFAULT 0"))
                db.session.commit()
        except Exception:
            db.session.rollback()

        # Миграция: добавляем колонку is_favorite в таблицу server, если её нет (для существующих БД)
        try:
            cols = [r[1] for r in db.session.execute(text("PRAGMA table_info(server)"))]
            if 'is_favorite' not in cols:
                db.session.execute(text("ALTER TABLE server ADD COLUMN is_favorite BOOLEAN DEFAULT 0"))
                db.session.commit()
        except Exception:
            db.session.rollback()
        
        # Миграция: добавляем таблицу settings, если её нет
        try:
            db.session.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='settings'"))
            if not db.session.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='settings'")).fetchone():
                db.create_all()
                # Добавляем настройки по умолчанию
                default_settings = [
                    Settings(key='theme', value='light', type='string', description='Цветовая тема (light/dark)'),
                    Settings(key='favicon_path', value='', type='string', description='Путь к файлу favicon'),
                    Settings(key='logo_path', value='', type='string', description='Путь к файлу логотипа'),
                    Settings(key='app_title', value='VNC Manager', type='string', description='Заголовок приложения'),
                    Settings(key='primary_color', value='#4a6cf7', type='string', description='Основной цвет темы'),
                    Settings(key='custom_css', value='', type='string', description='Пользовательские CSS стили')
                ]
                for setting in default_settings:
                    db.session.add(setting)
                db.session.commit()
        except Exception:
            db.session.rollback()
        
        # Создаем тестовые группы если их нет
        if Group.query.count() == 0:
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

def check_server_status(ip, port=5900):
    """Проверка статуса сервера VNC"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        result = sock.connect_ex((ip, port))
        sock.close()
        return result == 0
    except:
        return False

def check_printer_status(ip):
    """Проверка статуса принтера (поддержка Windows и Unix)"""
    try:
        system = platform.system()
        # В контейнерах/минимальных образах ping может отсутствовать.
        # В этом случае делаем быструю TCP-проверку на типичных портах принтера/веб-интерфейса.
        def _tcp_probe(host: str) -> bool:
            for p in (9100, 631, 515, 80, 443):
                try:
                    with socket.create_connection((host, p), timeout=1):
                        return True
                except Exception:
                    continue
            return False

        if system == 'Windows':
            # Windows использует другой синтаксис ping
            result = subprocess.run(['ping', '-n', '1', '-w', '1000', ip], 
                                  capture_output=True, text=True, timeout=2)
        else:
            # Unix/Linux/macOS
            result = subprocess.run(['ping', '-c', '1', '-W', '1', ip], 
                                  capture_output=True, text=True, timeout=2)
        return result.returncode == 0
    except:
        return _tcp_probe(ip)

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
                'printers_count': Printer.query.filter_by(group_id=group.id).count()
            }
            result.append(group_dict)
        return jsonify(result)
    
    elif request.method == 'POST':
        data = request.json
        
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
    
    db.session.delete(group)
    db.session.commit()
    return jsonify({'success': True})

# API для серверов
@app.route('/api/servers', methods=['GET'])
def get_servers():
    servers = Server.query.order_by(Server.name).all()
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
            'status': 'online' if check_server_status(server.ip, server.port) else 'offline'
        }
        
        if server.group:
            server_dict['group_name'] = server.group.name
            server_dict['group_color'] = server.group.color
        
        result.append(server_dict)
    
    return jsonify(result)

@app.route('/api/servers', methods=['POST'])
def add_server():
    data = request.json
    
    # Проверка на дубликат IP
    if Server.query.filter_by(ip=data['ip']).first():
        return jsonify({'error': 'IP адрес уже существует'}), 400
    
    server = Server(
        name=data['name'],
        ip=data['ip'],
        port=data.get('port', 5900),
        group_id=data.get('group_id'),
        comment=data.get('comment', '')
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
        data = request.json
        
        # Проверка на дубликат IP при изменении
        if 'ip' in data and data['ip'] != server.ip:
            if Server.query.filter_by(ip=data['ip']).first():
                return jsonify({'error': 'IP адрес уже существует'}), 400
        
        if 'name' in data:
            server.name = data['name']
        if 'ip' in data:
            server.ip = data['ip']
        if 'port' in data:
            server.port = data['port']
        if 'group_id' in data:
            server.group_id = data['group_id']
        if 'comment' in data:
            server.comment = data['comment']
        if 'is_favorite' in data:
            server.is_favorite = bool(data['is_favorite'])
        
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
        result = []
        for printer in printers:
            is_online = check_printer_status(printer.ip)
            printer_dict = {
                'id': printer.id,
                'name': printer.name,
                'ip': printer.ip,
                'group_id': printer.group_id,
                'web_interface': printer.web_interface,
                'is_favorite': bool(getattr(printer, 'is_favorite', False)),
                'status': 'online' if is_online else 'offline',
                'comment': printer.comment,
                'created_at': printer.created_at.isoformat() if printer.created_at else None
            }
            
            if printer.group:
                printer_dict['group_name'] = printer.group.name
                printer_dict['group_color'] = printer.group.color
            
            result.append(printer_dict)
        
        return jsonify(result)
    
    elif request.method == 'POST':
        data = request.json
        
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
        data = request.json
        
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

@app.route('/api/connect/<int:server_id>', methods=['POST'])
def connect_vnc(server_id):
    """Подключение к VNC серверу"""
    server = Server.query.get(server_id)
    if not server:
        return jsonify({'success': False, 'message': 'Сервер не найден'})
    
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
                    'status': 'online'
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
                            'web_interface': f'http{"s" if port == 443 else ""}://{ip}:{port}'
                        }
                        
                        # Добавляем подробную информацию, если доступна
                        device_data.update(printer_info)
                        
                        found_devices.append(device_data)
                        app.logger.info(f'Найден принтер: {ip}:{port} - {display_name}')
            
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
    
    def check_port(ip, port):
        """Проверка открытого порта"""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2)
            result = sock.connect_ex((ip, port))
            sock.close()
            app.logger.debug(f'Проверка порта {ip}:{port} - {"открыт" if result == 0 else "закрыт"}')
            return result == 0
        except Exception as e:
            app.logger.debug(f'Ошибка проверки порта {ip}:{port}: {e}')
            return False
    
    def is_likely_printer(ip, port):
        """Улучшенная проверка на принтер"""
        try:
            import urllib.request
            import urllib.error
            import re
            
            url = f'http{"s" if port == 443 else ""}://{ip}:{port}'
            req = urllib.request.Request(url, method='GET')
            
            with urllib.request.urlopen(req, timeout=5) as response:
                headers = dict(response.headers)
                content = response.read(5000).decode('utf-8', errors='ignore').lower()
                
                # Признаки принтера в HTTP заголовках
                server = headers.get('Server', '').lower()
                content_type = headers.get('Content-Type', '').lower()
                
                # Расширенный список ключевых слов принтеров
                printer_keywords = [
                    'printer', 'hp', 'canon', 'epson', 'brother', 'xerox',
                    'lexmark', 'samsung', 'kyocera', 'ricoh', 'panasonic',
                    'sharp', 'toshiba', 'konica', 'minolta', 'oki',
                    'dell', 'xerox', 'fuji', 'zebra', 'dymo'
                ]
                
                # Проверка заголовков
                header_match = any(keyword in server for keyword in printer_keywords)
                
                # Проверка Content-Type
                content_type_match = any(keyword in content_type for keyword in printer_keywords)
                
                # Проверка содержимого страницы (title, текст)
                title_match = False
                if '<title>' in content:
                    title_match = any(keyword in content for keyword in printer_keywords)
                
                # Проверка URL путей (часто в принтерах есть /printer, /status, /main)
                path_match = any(path in content for path in ['/printer', '/status', '/main', '/device', '/web'])
                
                # Если порт 9100 - скорее всего это принтер (стандартный порт печати)
                port_match = port == 9100
                
                # Достаточно любого совпадения
                is_printer = header_match or content_type_match or title_match or path_match or port_match
                
                if is_printer:
                    app.logger.info(f'Найден принтер {ip}:{port} - признаки: header={header_match}, content_type={content_type_match}, title={title_match}, path={path_match}, port={port_match}')
                
                return is_printer
                
        except Exception as e:
            app.logger.debug(f'Ошибка проверки принтера {ip}:{port}: {e}')
            return False
    
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
        clear_groups = bool(data.get('clear_groups', True))

        if clear_groups:
            # Если удаляем группы, сначала отвязываем устройства, чтобы не было проблем с FK.
            Server.query.update({'group_id': None}, synchronize_session=False)
            Printer.query.update({'group_id': None}, synchronize_session=False)

        if clear_servers:
            Server.query.delete(synchronize_session=False)
        if clear_printers:
            Printer.query.delete(synchronize_session=False)
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

    # Минимальная страница noVNC. Библиотеку берём с CDN, прокси — websockify на NOVNC_PROXY_PORT.
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>noVNC — {server.name}</title>
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
    .wrap {{ display: grid; grid-template-rows: auto 1fr; height: 100%; }}
    .top {{
      display: flex; gap: 12px; align-items: center; justify-content: space-between;
      padding: 10px 12px; background: var(--panel); border-bottom: 1px solid var(--border);
    }}
    .title {{ display: flex; flex-direction: column; gap: 2px; min-width: 0; }}
    .title strong {{ font-size: 14px; }}
    .title span {{ font-size: 12px; color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .actions {{ display: flex; gap: 8px; align-items: center; flex-shrink: 0; }}
    button {{
      height: 34px; border-radius: 8px; border: 1px solid var(--border); cursor: pointer;
      padding: 0 10px; color: var(--text); background: var(--btn2);
    }}
    button.primary {{ background: var(--btn); border-color: rgba(37,99,235,.6); }}
    #status {{ font-size: 12px; color: var(--muted); }}
    #status.ok {{ color: var(--ok); }}
    #status.bad {{ color: var(--bad); }}
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
  </style>
</head>
<body>
  <div class="wrap">
    <div class="top">
      <div class="title">
        <strong>noVNC — {server.name}</strong>
        <span>{server.ip}:{server.port}</span>
      </div>
      <div class="actions">
        <div id="status">Подготовка…</div>
        <button id="btnReconnect" class="primary">Подключиться</button>
        <button id="btnDisconnect">Отключить</button>
      </div>
    </div>
    <div id="screen"></div>
  </div>

  <script type="module">
    import RFB from 'https://cdn.jsdelivr.net/npm/@novnc/novnc@latest/core/rfb.js';

    const serverId = {server.id};
    const statusEl = document.getElementById('status');
    const screen = document.getElementById('screen');
    const btnReconnect = document.getElementById('btnReconnect');
    const btnDisconnect = document.getElementById('btnDisconnect');

    let rfb = null;
    let ro = null;

    function setStatus(text, kind) {{
      statusEl.textContent = text;
      statusEl.classList.remove('ok', 'bad');
      if (kind) statusEl.classList.add(kind);
    }}

    async function connect() {{
      try {{
        setStatus('Получаю токен…');
        const resp = await fetch(`/api/novnc/${{serverId}}/token`, {{ method: 'POST' }});
        const data = await resp.json();
        if (!resp.ok || !data.success) {{
          throw new Error(data.message || 'Не удалось получить токен');
        }}

        const scheme = (location.protocol === 'https:') ? 'wss' : 'ws';
        const host = location.hostname;
        const wsUrl = `${{scheme}}://${{host}}:${{data.proxy_port}}/?token=${{encodeURIComponent(data.token)}}`;

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
          const password = prompt('Введите пароль VNC (если требуется):') || '';
          try {{
            rfb.sendCredentials({{ password }});
          }} catch (e) {{
            setStatus('Не удалось отправить пароль', 'bad');
          }}
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

    function disconnect() {{
      if (rfb) {{
        try {{ rfb.disconnect(); }} catch (_) {{}}
        rfb = null;
      }}
      if (ro) {{
        try {{ ro.disconnect(); }} catch (_) {{}}
        ro = null;
      }}
      setStatus('Отключено');
    }}

    btnReconnect.addEventListener('click', connect);
    btnDisconnect.addEventListener('click', disconnect);
    window.addEventListener('resize', () => {{
      if (rfb) rfb.scaleViewport = true;
    }});

    // Автоподключение
    connect();
  </script>
</body>
</html>"""

@app.route('/api/export', methods=['GET'])
def export_data():
    """Экспорт всех данных в JSON"""
    try:
        groups = Group.query.all()
        servers = Server.query.all()
        printers = Printer.query.all()
        
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
                'status': 'online' if check_server_status(server.ip, server.port) else 'offline'
            })
        
        printers_data = []
        for printer in printers:
            printers_data.append({
                'id': printer.id,
                'name': printer.name,
                'ip': printer.ip,
                'group_id': printer.group_id,
                'web_interface': printer.web_interface,
                'status': 'online' if printer.status else 'offline',
                'comment': printer.comment,
                'created_at': printer.created_at.isoformat() if printer.created_at else None
            })
        
        export_data = {
            'metadata': {
                'exported_at': datetime.now().isoformat(),
                'version': '2.0',
                'total_servers': len(servers_data),
                'total_printers': len(printers_data),
                'total_groups': len(groups_data)
            },
            'groups': groups_data,
            'servers': servers_data,
            'printers': printers_data
        }
        
        return jsonify(export_data)
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/import', methods=['POST'])
def import_data():
    """Импорт данных из JSON"""
    try:
        data = request.json
        
        if not data:
            return jsonify({'error': 'Нет данных для импорта'}), 400
        
        mode = data.get('mode', 'merge')
        
        if mode == 'overwrite':
            Server.query.delete()
            Printer.query.delete()
            Group.query.delete()
            db.session.commit()
        
        groups_data = data.get('groups', [])
        group_map = {}
        
        for group_data in groups_data:
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
            group_id = group_map.get(server_data.get('group_id')) if server_data.get('group_id') else None
            
            if Server.query.filter_by(ip=server_data['ip']).first():
                # Обновляем существующий сервер
                server = Server.query.filter_by(ip=server_data['ip']).first()
                server.name = server_data['name']
                server.port = server_data.get('port', 5900)
                server.group_id = group_id
                server.is_favorite = server_data.get('is_favorite', False)
                server.comment = server_data.get('comment', '')
            else:
                # Создаем новый сервер
                server = Server(
                    name=server_data['name'],
                    ip=server_data['ip'],
                    port=server_data.get('port', 5900),
                    group_id=group_id,
                    is_favorite=server_data.get('is_favorite', False),
                    comment=server_data.get('comment', '')
                )
                db.session.add(server)
        
        printers_data = data.get('printers', [])
        for printer_data in printers_data:
            group_id = group_map.get(printer_data.get('group_id')) if printer_data.get('group_id') else None
            
            if Printer.query.filter_by(ip=printer_data['ip']).first():
                # Обновляем существующий принтер
                printer = Printer.query.filter_by(ip=printer_data['ip']).first()
                printer.name = printer_data['name']
                printer.group_id = group_id
                printer.web_interface = printer_data.get('web_interface', '')
                printer.comment = printer_data.get('comment', '')
            else:
                # Создаем новый принтер
                printer = Printer(
                    name=printer_data['name'],
                    ip=printer_data['ip'],
                    group_id=group_id,
                    web_interface=printer_data.get('web_interface', ''),
                    comment=printer_data.get('comment', '')
                )
                db.session.add(printer)
        
        db.session.commit()
        
        return jsonify({'success': True, 'message': 'Данные успешно импортированы'})
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

# API для настроек
@app.route('/api/settings', methods=['GET'])
def get_settings():
    """Получить все настройки"""
    settings = Settings.query.all()
    result = {}
    for setting in settings:
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
        data = request.json
        
        for key, value in data.items():
            setting = Settings.query.filter_by(key=key).first()
            
            if setting:
                # Обновляем существующую настройку
                if setting.type == 'boolean':
                    setting.value = 'true' if value else 'false'
                elif setting.type == 'number':
                    setting.value = str(value)
                else:
                    setting.value = str(value)
                setting.updated_at = datetime.utcnow()
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

@app.route('/api/settings/upload_favicon', methods=['POST'])
def upload_favicon():
    """Загрузить favicon"""
    try:
        if 'favicon' not in request.files:
            return jsonify({'error': 'Файл не загружен'}), 400
        
        file = request.files['favicon']
        if file.filename == '':
            return jsonify({'error': 'Файл не выбран'}), 400
        
        # Проверяем расширение файла
        allowed_extensions = {'ico', 'png', 'jpg', 'jpeg', 'gif', 'svg'}
        if not ('.' in file.filename and 
                file.filename.rsplit('.', 1)[1].lower() in allowed_extensions):
            return jsonify({'error': 'Неверный формат файла. Разрешены: ico, png, jpg, jpeg, gif, svg'}), 400
        
        # Создаем директорию для статических файлов, если её нет
        static_dir = os.path.join(app.static_folder, 'uploads')
        os.makedirs(static_dir, exist_ok=True)
        
        # Сохраняем файл
        filename = f"favicon.{file.filename.rsplit('.', 1)[1].lower()}"
        file_path = os.path.join(static_dir, filename)
        file.save(file_path)
        
        # Обновляем настройку в базе данных
        setting = Settings.query.filter_by(key='favicon_path').first()
        if setting:
            setting.value = f"uploads/{filename}"
            setting.updated_at = datetime.utcnow()
        else:
            setting = Settings(
                key='favicon_path',
                value=f"uploads/{filename}",
                type='string',
                description='Путь к файлу favicon'
            )
            db.session.add(setting)
        
        db.session.commit()
        return jsonify({'success': True, 'path': f"uploads/{filename}"})
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

@app.route('/api/settings/upload_logo', methods=['POST'])
def upload_logo():
    """Загрузить логотип"""
    try:
        if 'logo' not in request.files:
            return jsonify({'error': 'Файл не загружен'}), 400
        
        file = request.files['logo']
        if file.filename == '':
            return jsonify({'error': 'Файл не выбран'}), 400
        
        # Проверяем расширение файла
        allowed_extensions = {'png', 'jpg', 'jpeg', 'gif', 'svg', 'webp'}
        if not ('.' in file.filename and 
                file.filename.rsplit('.', 1)[1].lower() in allowed_extensions):
            return jsonify({'error': 'Неверный формат файла. Разрешены: png, jpg, jpeg, gif, svg, webp'}), 400
        
        # Создаем директорию для статических файлов, если её нет
        static_dir = os.path.join(app.static_folder, 'uploads')
        os.makedirs(static_dir, exist_ok=True)
        
        # Сохраняем файл
        filename = f"logo.{file.filename.rsplit('.', 1)[1].lower()}"
        file_path = os.path.join(static_dir, filename)
        file.save(file_path)
        
        # Обновляем настройку в базе данных
        setting = Settings.query.filter_by(key='logo_path').first()
        if setting:
            setting.value = f"uploads/{filename}"
            setting.updated_at = datetime.utcnow()
        else:
            setting = Settings(
                key='logo_path',
                value=f"uploads/{filename}",
                type='string',
                description='Путь к файлу логотипа'
            )
            db.session.add(setting)
        
        db.session.commit()
        return jsonify({'success': True, 'path': f"uploads/{filename}"})
        
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
import os
import json
from datetime import datetime

@app.route('/api/create_backup', methods=['POST'])
def create_backup():
    try:
        data = request.get_json()
        
        # Создаем имя файла с временной меткой
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"backup_{timestamp}.json"
        backup_path = os.path.join('backups', filename)
        
        # Убеждаемся, что папка существует
        os.makedirs('backups', exist_ok=True)
        
        # Сохраняем бэкап
        with open(backup_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        
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
        backups_dir = 'backups'
        if not os.path.exists(backups_dir):
            return jsonify({'success': True, 'backups': []})
        
        backups = []
        for filename in os.listdir(backups_dir):
            if filename.endswith('.json'):
                filepath = os.path.join(backups_dir, filename)
                stat = os.stat(filepath)
                
                # Читаем метаданные из файла
                try:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        backup_data = json.load(f)
                        app_title = backup_data.get('appTitle', 'VNC Manager')
                        timestamp = backup_data.get('timestamp', '')
                        comment = backup_data.get('comment', '')
                except:
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
        
        # Сортируем по времени создания (новые первые)
        backups.sort(key=lambda x: x['created'], reverse=True)
        
        return jsonify({'success': True, 'backups': backups})
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/restore_backup/<filename>', methods=['POST'])
def restore_backup(filename):
    try:
        backup_path = os.path.join('backups', filename)
        
        if not os.path.exists(backup_path):
            return jsonify({'error': 'Файл бэкапа не найден'}), 404
        
        # Читаем данные из бэкапа
        with open(backup_path, 'r', encoding='utf-8') as f:
            backup_data = json.load(f)
        
        # Восстанавливаем данные
        # Здесь можно добавить логику восстановления разных типов данных
        
        return jsonify({
            'success': True,
            'message': f'Данные восстановлены из {filename}'
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/delete_backups', methods=['POST'])
def delete_backups():
    try:
        data = request.get_json()
        filenames = data.get('filenames', [])
        
        if not filenames:
            return jsonify({'error': 'Не указаны файлы для удаления'}), 400
        
        deleted_count = 0
        errors = []
        
        for filename in filenames:
            backup_path = os.path.join('backups', filename)
            try:
                if os.path.exists(backup_path):
                    os.remove(backup_path)
                    deleted_count += 1
                else:
                    errors.append(f'Файл {filename} не найден')
            except Exception as e:
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
    
    app.run(debug=True, host='0.0.0.0', port=5000, use_reloader=False)
