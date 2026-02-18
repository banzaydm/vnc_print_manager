#!/usr/bin/env python3
"""
Установщик зависимостей для VNC & Printer Manager
"""

import sys
import subprocess
import os

def install_requirements():
    """Установка зависимостей из requirements.txt"""
    print("=" * 50)
    print("Установка зависимостей для VNC & Printer Manager")
    print("=" * 50)
    
    # Проверяем, есть ли requirements.txt
    if not os.path.exists('requirements.txt'):
        print("❌ Файл requirements.txt не найден!")
        return False
    
    try:
        # Читаем зависимости
        with open('requirements.txt', 'r') as f:
            requirements = [line.strip() for line in f if line.strip() and not line.startswith('#')]
        
        if not requirements:
            print("⚠️  Файл requirements.txt пуст")
            return True
        
        print(f"📦 Найдено {len(requirements)} зависимостей:")
        for req in requirements:
            print(f"  - {req}")
        
        # Устанавливаем зависимости
        print("\n🚀 Устанавливаем зависимости...")
        for requirement in requirements:
            print(f"\n📦 Установка: {requirement}")
            try:
                subprocess.check_call([sys.executable, '-m', 'pip', 'install', requirement])
                print(f"✅ Успешно установлено: {requirement}")
            except subprocess.CalledProcessError as e:
                print(f"❌ Ошибка установки {requirement}: {e}")
                return False
        
        print("\n" + "=" * 50)
        print("✅ Все зависимости успешно установлены!")
        print("=" * 50)
        return True
        
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        return False

def check_python_version():
    """Проверка версии Python"""
    print("🔍 Проверка версии Python...")
    if sys.version_info < (3, 7):
        print("❌ Требуется Python 3.7 или выше")
        print(f"   У вас установлен: {sys.version}")
        return False
    print(f"✅ Python {sys.version} - OK")
    return True

def check_pip():
    """Проверка наличия pip"""
    print("🔍 Проверка наличия pip...")
    try:
        subprocess.check_call([sys.executable, '-m', 'pip', '--version'])
        print("✅ pip доступен - OK")
        return True
    except:
        print("❌ pip не найден. Установите pip:")
        print("   Для macOS/Linux: python3 -m ensurepip")
        print("   Или скачайте с: https://pip.pypa.io/en/stable/installation/")
        return False

def create_virtual_env():
    """Создание виртуального окружения (опционально)"""
    print("\n🌐 Хотите создать виртуальное окружение? (y/N): ", end="")
    choice = input().strip().lower()
    
    if choice in ['y', 'yes', 'д', 'да']:
        print("🔧 Создаем виртуальное окружение...")
        try:
            # Создаем виртуальное окружение
            subprocess.check_call([sys.executable, '-m', 'venv', 'venv'])
            
            # Определяем путь к pip в виртуальном окружении
            if sys.platform == 'win32':
                pip_path = os.path.join('venv', 'Scripts', 'pip')
                python_path = os.path.join('venv', 'Scripts', 'python')
            else:
                pip_path = os.path.join('venv', 'bin', 'pip')
                python_path = os.path.join('venv', 'bin', 'python')
            
            print(f"✅ Виртуальное окружение создано в папке 'venv'")
            print(f"📝 Для активации:")
            
            if sys.platform == 'win32':
                print("   Windows: venv\\Scripts\\activate")
                print(f"   Затем запустите: {python_path} run.py")
            else:
                print("   macOS/Linux: source venv/bin/activate")
                print(f"   Затем запустите: {python_path} run.py")
            
            # Устанавливаем зависимости в виртуальное окружение
            print("\n📦 Устанавливаем зависимости в виртуальное окружение...")
            with open('requirements.txt', 'r') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#'):
                        subprocess.check_call([pip_path, 'install', line])
            
            print("✅ Зависимости установлены в виртуальное окружение")
            return True
            
        except Exception as e:
            print(f"❌ Ошибка создания виртуального окружения: {e}")
            print("⚠️  Продолжаем установку в глобальное окружение")
    
    return False

def main():
    """Основная функция установки"""
    print("=" * 50)
    print("VNC & Printer Manager - Установка")
    print("=" * 50)
    
    # Проверяем версию Python
    if not check_python_version():
        return 1
    
    # Проверяем наличие pip
    if not check_pip():
        return 1
    
    # Предлагаем создать виртуальное окружение
    create_virtual_env()
    
    # Устанавливаем зависимости
    if not install_requirements():
        return 1
    
    print("\n🎉 Установка завершена!")
    print("\n📝 Для запуска приложения:")
    print("   1. Запустите сервер: python run.py")
    print("   2. Откройте браузер: http://localhost:5000")
    print("\n💡 Или используйте команду: python run.py")
    
    # Запускаем приложение после установки?
    print("\n🚀 Запустить приложение сейчас? (y/N): ", end="")
    choice = input().strip().lower()
    
    if choice in ['y', 'yes', 'д', 'да']:
        print("\nЗапускаем приложение...")
        try:
            import run
        except ImportError:
            print("⚠️  Не удалось запустить приложение автоматически")
            print("   Запустите вручную: python run.py")
    
    return 0

if __name__ == '__main__':
    sys.exit(main())