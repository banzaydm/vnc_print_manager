# -*- coding: utf-8 -*-
"""Потоки камер: управление HLS-трансляцией, отдача сегментов и превью."""
import os
import subprocess
import time

from flask import Blueprint, abort, jsonify, render_template_string, request, send_file

from models import Camera
from services.hls import (
    _HLS_DIR, _HLS_LOCK, _HLS_PROCESSES,
    _ffmpeg_bin, _hls_ffmpeg_available, _rtsp_url_with_creds,
)

bp = Blueprint('streams', __name__)
@bp.route('/api/camera/stream/<int:camera_id>', methods=['POST', 'DELETE'])
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
            _ffmpeg_bin(), '-hide_banner', '-loglevel', 'error',
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


@bp.route('/api/camera/stream/<int:camera_id>/<path:filename>', methods=['GET'])
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


@bp.route('/camera_view/<int:camera_id>')
def camera_view_page(camera_id):
    camera = Camera.query.get(camera_id)
    if not camera:
        abort(404)
    return render_template_string(_CAMERA_VIEW_HTML, camera=camera)


