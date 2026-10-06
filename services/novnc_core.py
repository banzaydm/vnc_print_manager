# -*- coding: utf-8 -*-
"""noVNC/websockify: константы, файл токенов и учёт живых токенов."""
import os
import threading
import time

NOVNC_PROXY_PORT = int(os.environ.get("NOVNC_PROXY_PORT", "6080"))
NOVNC_TOKEN_TTL_SECONDS = int(os.environ.get("NOVNC_TOKEN_TTL_SECONDS", "600"))
NOVNC_WS_PATH = os.environ.get("NOVNC_WS_PATH", "").strip()
NOVNC_CDN_VERSION = os.environ.get("NOVNC_CDN_VERSION", "1.5.0")
# instance_path по умолчанию = <корень пакета>/instance (исходник лежит в корне),
# поэтому путь считаем от расположения пакета, а не от объекта Flask.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_NOVNC_TOKEN_FILE = os.path.join(_PROJECT_ROOT, "instance", "novnc_tokens.txt")

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
