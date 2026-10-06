"""RTSP -> HLS: состояние ffmpeg-процессов и вспомогательные функции.

_HLS_DIR лежит в instance/hls корня проекта (не в services/): путь считается
от расположения этого файла, поэтому при переносе кода путь не «уехал».
"""
import os
import re
import subprocess
import threading
from urllib.parse import quote

# --- RTSP->HLS просмотр камер в браузере ---
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_HLS_DIR = os.path.join(_PROJECT_ROOT, 'instance', 'hls')
_HLS_LOCK = threading.Lock()
_HLS_PROCESSES = {}  # camera_id -> Popen

_FFMPEG_BIN = None


def _ffmpeg_bin():
    """Путь к FFmpeg: статический бинарь из imageio-ffmpeg (pip), иначе 'ffmpeg' из PATH."""
    global _FFMPEG_BIN
    if _FFMPEG_BIN is None:
        try:
            import imageio_ffmpeg
            _FFMPEG_BIN = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            _FFMPEG_BIN = 'ffmpeg'
    return _FFMPEG_BIN


def _hls_ffmpeg_available():
    try:
        r = subprocess.run([_ffmpeg_bin(), '-version'], capture_output=True, timeout=5)
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
