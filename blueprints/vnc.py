# -*- coding: utf-8 -*-
"""Подключение к VNC: настольный клиент (find_vnc_client) и страница noVNC."""
import json
import os
import platform
import secrets
import subprocess
import time

from flask import Blueprint, jsonify
from markupsafe import escape

from models import Server

from services.novnc_core import (
    NOVNC_CDN_VERSION, NOVNC_PROXY_PORT, NOVNC_TOKEN_TTL_SECONDS, NOVNC_WS_PATH,
    _novnc_lock, _novnc_tokens,
    _prune_novnc_tokens_locked, _write_novnc_token_file_locked,
)
from services.status_cache import pick_reachable_endpoint

bp = Blueprint('vnc', __name__)

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

@bp.route('/api/connect/<int:server_id>', methods=['POST'])
def connect_vnc(server_id):
    """Подключение к VNC серверу"""
    server = Server.query.get(server_id)
    if not server:
        return jsonify({'success': False, 'message': 'Сервер не найден'}), 404

    # Если основной адрес недоступен, а запасной жив — подключаемся по запасному
    host, port = pick_reachable_endpoint(server.ip, server.port, server.alt_endpoints)

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
                f'Адрес: {host}:{port}'
            ]
        })
    
    try:
        if platform.system() == 'Darwin':
            if 'Screen Sharing' in vnc_path:
                subprocess.Popen(['open', f"vnc://{host}:{port}"])
            else:
                subprocess.Popen([vnc_path, f"{host}:{port}"])
        else:
            subprocess.Popen([vnc_path, f"{host}:{port}"])
        
        return jsonify({
            'success': True,
            'message': f'Открываю подключение к {host}:{port} через {client_name}'
        })
        
    except Exception as e:
        return jsonify({
            'success': False,
            'message': f'Ошибка при запуске VNC клиента: {str(e)}',
            'manual_connection': f"Вы можете подключиться вручную: {host}:{port}"
        })


@bp.route('/api/novnc/<int:server_id>/token', methods=['POST'])
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

    # Токен мапится на живой адрес (основной или запасной)
    host, port = pick_reachable_endpoint(server.ip, server.port, server.alt_endpoints)

    with _novnc_lock:
        _prune_novnc_tokens_locked(now)
        _novnc_tokens[token] = (host, port, expires_at)
        _write_novnc_token_file_locked()

    return jsonify(
        {
            "success": True,
            "token": token,
            "proxy_port": NOVNC_PROXY_PORT,
            "expires_in_seconds": NOVNC_TOKEN_TTL_SECONDS,
            "server": {"id": server.id, "name": server.name, "ip": host, "port": port},
        }
    )


@bp.route('/novnc/<int:server_id>')
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
    /* При вложенных VNC-сессиях сервер может не передавать изображение курсора,
       и noVNC прячет системный курсор (cursor: none) — курсор становится невидим.
       По умолчанию показываем системный курсор браузера; режим "серверный" курсор
       (body.server-cursor) возвращает стандартное поведение noVNC. */
    body:not(.server-cursor) #screen canvas {{
      cursor: default !important;
    }}
    .cursor-toggle {{
      position: fixed; top: 46px; right: 10px; z-index: 50;
      font-size: 11px; height: auto; padding: 4px 8px;
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
    <button id="btnCursor" class="cursor-toggle">Курсор: системный</button>
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

    // Переключение курсора: системный (по умолчанию, не пропадает при
    // вложенном VNC) либо серверный (стандартное поведение noVNC).
    const btnCursor = document.getElementById('btnCursor');
    btnCursor.addEventListener('click', () => {{
      const serverCursor = document.body.classList.toggle('server-cursor');
      btnCursor.textContent = serverCursor ? 'Курсор: серверный' : 'Курсор: системный';
    }});

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
