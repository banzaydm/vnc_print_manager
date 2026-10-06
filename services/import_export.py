"""Экспорт/импорт данных: конвертация payload и само копирование в БД."""
from models import (
    db, Group, Server, Printer, Camera, Router, Settings, SubnetName, utcnow,
)
from services.device_fields import _apply_password

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
