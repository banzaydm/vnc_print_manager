#!/usr/bin/env python3
"""
Скрипт для запуска приложения VNC & Printer Manager
"""

import sys
import os
import webbrowser
import subprocess
from threading import Timer

_websockify_proc = None


def check_dependencies():
    """Проверка установленных зависимостей"""
    try:
        import flask
        print("✅ Flask установлен")
        import flask_sqlalchemy
        print("✅ Flask-SQLAlchemy установлен")
        import websockify
        print("✅ websockify установлен (для noVNC)")
        return True
    except ImportError as e:
        print(f"❌ Не найдена зависимость: {e.name}")
        print("\nУстановите зависимости командой:")
        print("  pip install -r requirements.txt")
        print("\nИли запустите установщик:")
        print("  python install.py")
        return False


def start_websockify():
    """Запуск websockify для noVNC (локальная разработка)."""
    global _websockify_proc
    if os.environ.get('START_WEBSOCKIFY', '1').strip().lower() in {'0', 'false', 'no', 'off'}:
        return None

    from app import app, NOVNC_PROXY_PORT, _NOVNC_TOKEN_FILE

    cmd = [
        sys.executable, '-m', 'websockify',
        '--token-plugin', 'TokenFile',
        '--token-source', _NOVNC_TOKEN_FILE,
        str(NOVNC_PROXY_PORT),
    ]
    try:
        _websockify_proc = subprocess.Popen(cmd, cwd=os.path.dirname(__file__))
        print(f"✅ websockify запущен на порту {NOVNC_PROXY_PORT}")
        return _websockify_proc
    except OSError as e:
        print(f"⚠️  Не удалось запустить websockify: {e}")
        return None


def main():
    print("=" * 50)
    print("VNC & Printer Manager - Запуск")
    print("=" * 50)

    if not check_dependencies():
        print("\n❌ Не удалось запустить приложение: отсутствуют зависимости")
        sys.exit(1)

    from app import app

    start_websockify()

    def open_browser():
        try:
            webbrowser.open('http://localhost:5000')
        except OSError:
            print("⚠️  Не удалось открыть браузер автоматически")

    Timer(2, open_browser).start()

    print("\n✅ Зависимости проверены успешно")
    print("🚀 Запускаем сервер...")
    print("\n📱 Приложение будет доступно по адресу: http://localhost:5000")
    print("🛑 Для остановки нажмите Ctrl+C")
    print("=" * 50)

    debug = os.environ.get('FLASK_DEBUG', '0').strip().lower() in {'1', 'true', 'yes', 'on'}

    try:
        app.run(debug=debug, host='0.0.0.0', port=5000, use_reloader=False)
    except Exception as e:
        print(f"\n❌ Ошибка запуска сервера: {e}")
        sys.exit(1)
    finally:
        if _websockify_proc and _websockify_proc.poll() is None:
            _websockify_proc.terminate()


if __name__ == '__main__':
    main()
