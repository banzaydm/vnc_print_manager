# -*- coding: utf-8 -*-
"""CRUD групп, серверов, принтеров, камер, роутеров и избранного."""

from flask import Blueprint, jsonify, request

from models import Camera, Group, Printer, Router, Server, db
from services.api_utils import get_json, _normalize_port
from services.device_fields import _apply_password
from services.status_cache import _fetch_statuses_parallel

bp = Blueprint('devices', __name__)
# API для групп
@bp.route('/api/groups', methods=['GET', 'POST'])
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

@bp.route('/api/groups/<int:group_id>', methods=['DELETE'])
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
@bp.route('/api/servers', methods=['GET'])
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

@bp.route('/api/servers', methods=['POST'])
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

@bp.route('/api/servers/<int:server_id>', methods=['PUT', 'DELETE'])
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
@bp.route('/api/printers', methods=['GET', 'POST'])
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

@bp.route('/api/printers/<int:printer_id>', methods=['PUT', 'DELETE'])
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

@bp.route('/api/cameras', methods=['GET', 'POST'])
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

@bp.route('/api/cameras/<int:camera_id>', methods=['PUT', 'DELETE'])
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

@bp.route('/api/routers', methods=['GET', 'POST'])
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

@bp.route('/api/routers/<int:router_id>', methods=['PUT', 'DELETE'])
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


@bp.route('/api/favorites/<string:device_type>/<int:device_id>', methods=['PUT', 'POST'])
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


