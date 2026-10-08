# -*- coding: utf-8 -*-
"""Сканирование сети: запуск фонового скана, прогресс и отмена."""
import logging

from flask import Blueprint, jsonify, request

from services import scan_runner

bp = Blueprint('scan', __name__)
logger = logging.getLogger(__name__)


@bp.route('/api/scan', methods=['POST'])
def scan_start():
    """Запуск сканирования в фоне; возвращает scan_id и размер пула."""
    data = request.json or {}
    try:
        job, already_running = scan_runner.start(data.get('range'))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    logger.info(f'Запрос на сканирование {job["range"]} ({job["total"]} IP), '
                f'уже выполняется: {already_running}')
    return jsonify({
        'success': True,
        'scan_id': job['id'],
        'total': job['total'],
        'already_running': already_running
    })


@bp.route('/api/scan/status', methods=['GET'])
def scan_status():
    """Прогресс и частичные результаты текущего/последнего сканирования."""
    return jsonify(scan_runner.status())


@bp.route('/api/scan/cancel', methods=['POST'])
def scan_cancel():
    """Остановка текущего сканирования."""
    if not scan_runner.cancel():
        return jsonify({'success': False, 'error': 'Сканирование не выполняется'}), 409
    return jsonify({'success': True, 'status': 'cancelling'})
