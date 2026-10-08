# -*- coding: utf-8 -*-
"""Страница подключения и API панели RustDesk (lejianwen/rustdesk-api)."""
import json
import time

from flask import Blueprint, jsonify
from markupsafe import escape

from models import Server, db
from services.api_utils import get_json, _normalize_port
from services.rustdesk_api import _RD_ONLINE_WINDOW, _rd_fetch_peers_cached
from services.settings_store import _get_setting

bp = Blueprint('rustdesk', __name__)
@bp.route('/rustdesk/<int:server_id>')
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


@bp.route('/api/rustdesk/status', methods=['GET'])
def rustdesk_status():
    """Онлайн-статусы устройств по RustDesk ID (лёгкий ответ для кнопок).

    configured=false — панель не настроена, фронтенд просто не показывает
    индикаторы. Ошибки связи возвращаются как success=false, чтобы клиент
    сохранил последнее известное состояние.
    """
    api_url = (_get_setting('rustdesk_api_url', '') or '').strip()
    username = (_get_setting('rustdesk_api_user', '') or '').strip()
    password = _get_setting('rustdesk_api_pass', '')
    if not api_url or not username or not password:
        return jsonify({'success': True, 'configured': False, 'status': {}})

    peers, err, code = _rd_fetch_peers_cached(api_url, username, password)
    if err:
        return jsonify({'success': False, 'error': err}), code or 502

    now = time.time()
    status = {}
    by_ip = {}
    for p in peers:
        last_ts = int(p.get('last_online_time') or 0)
        online = bool(last_ts) and (now - last_ts) < _RD_ONLINE_WINDOW
        rd_id = (p.get('id') or '').strip()
        if rd_id:
            status[rd_id] = {'online': online, 'last': last_ts}
        # Индекс по последнему IP: у сервера RustDesk ID может быть не заполнен,
        # тогда статус всё равно находится по совпадению IP (как в списках панели).
        ip = (p.get('last_online_ip') or '').strip()
        if ip:
            cur = by_ip.get(ip)
            if cur is None or last_ts > cur['last']:
                by_ip[ip] = {'online': online, 'last': last_ts}
    return jsonify({
        'success': True,
        'configured': True,
        'status': status,
        'by_ip': by_ip,
        'window': _RD_ONLINE_WINDOW,
    })


@bp.route('/api/rustdesk/devices', methods=['GET'])
def rustdesk_devices():
    """Список устройств из панели RustDesk API (lejianwen/rustdesk-api)."""
    api_url = (_get_setting('rustdesk_api_url', '') or '').strip()
    username = (_get_setting('rustdesk_api_user', '') or '').strip()
    password = _get_setting('rustdesk_api_pass', '')
    if not api_url or not username or not password:
        return jsonify({'success': False,
                        'error': 'Настройки RustDesk API не заполнены (адрес панели, логин, пароль).'}), 400

    peers, err, code = _rd_fetch_peers_cached(api_url, username, password)
    if err:
        return jsonify({'success': False, 'error': err}), code or 502

    now = time.time()

    # Карты привязок и совпадений по IP для кнопки «Добавить»
    # Индексируем все адреса сервера (основной + запасные)
    linked_ids = set()
    ip_server_map = {}
    for s in Server.query.all():
        if s.rustdesk_id:
            linked_ids.add(s.rustdesk_id)
        for host, _port in s.all_endpoints():
            if host and host not in ip_server_map:
                ip_server_map[host] = s

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
            'total': len(devices),
    })

@bp.route('/api/rustdesk/add', methods=['POST'])
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

