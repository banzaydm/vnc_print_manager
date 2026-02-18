#!/usr/bin/env python3
"""
Скрипт для запуска приложения VNC & Printer Manager
"""

import sys
import webbrowser
from threading import Timer

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

def main():
    print("=" * 50)
    print("VNC & Printer Manager - Запуск")
    print("=" * 50)
    
    # Проверяем зависимости
    if not check_dependencies():
        print("\n❌ Не удалось запустить приложение: отсутствуют зависимости")
        sys.exit(1)
    
    # Импортируем приложение только после проверки зависимостей
    from app import app
    
    # Открываем браузер через 2 секунды
    def open_browser():
        try:
            webbrowser.open('http://localhost:5000')
        except:
            print("⚠️  Не удалось открыть браузер автоматически")
    
    Timer(2, open_browser).start()
    
    print("\n✅ Зависимости проверены успешно")
    print("🚀 Запускаем сервер...")
    print("\n📱 Приложение будет доступно по адресу: http://localhost:5000")
    print("🛑 Для остановки нажмите Ctrl+C")
    print("=" * 50)
    
    # Запускаем приложение
    try:
        app.run(debug=True, host='0.0.0.0', port=5000, use_reloader=False)
    except Exception as e:
        print(f"\n❌ Ошибка запуска сервера: {e}")
        sys.exit(1)

if __name__ == '__main__':
    main()