# -*- coding: utf-8 -*-
"""Экспорт/импорт данных, резервные копии и очистка базы."""
import json
import os
from datetime import datetime

from flask import Blueprint, current_app, jsonify, request

from models import db, Group, Server, Printer, Camera, Router, Settings, SubnetName
from services.api_utils import get_json
from services.backups import BACKUP_FILENAME_RE, safe_backup_path
from services.import_export import _backup_to_import_payload, import_data_core
from services.settings_store import _get_setting
from services.status_cache import _fetch_statuses_parallel

bp = Blueprint('data', __name__)


def _backups_dir():
    return os.path.join(current_app.instance_path, 'backups')


def _safe_backup_path(filename: str):
    return safe_backup_path(filename, _backups_dir())
@bp.route('/api/admin/clear_db', methods=['POST', 'PUT'])
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


@bp.route('/api/export', methods=['GET'])
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


@bp.route('/api/import', methods=['POST'])
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



# API для бэкапов и восстановления

@bp.route('/api/create_backup', methods=['POST'])
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
        backup_path = os.path.join(_backups_dir(), filename)

        with open(backup_path, 'w', encoding='utf-8') as f:
            json.dump(backup_data, f, ensure_ascii=False, indent=2)

        return jsonify({
            'success': True,
            'filename': filename,
            'message': f'Backup создан: {filename}'
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@bp.route('/api/backups', methods=['GET'])
def list_backups():
    try:
        if not os.path.exists(_backups_dir()):
            return jsonify({'success': True, 'backups': []})

        backups = []
        for filename in os.listdir(_backups_dir()):
            if not BACKUP_FILENAME_RE.fullmatch(filename):
                continue
            filepath = os.path.join(_backups_dir(), filename)
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


@bp.route('/api/restore_backup/<filename>', methods=['POST'])
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


@bp.route('/api/delete_backups', methods=['POST'])
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

