#!/bin/bash

echo "========================================"
echo "VNC & Printer Manager - Установка"
echo "========================================"
echo ""

# Проверяем Python
if ! command -v python3 &> /dev/null; then
    echo "Ошибка: Python 3 не найден!"
    echo "Установите Python 3.7 или выше:"
    echo "macOS: brew install python3"
    echo "Linux: sudo apt install python3"
    exit 1
fi

# Проверяем версию Python
python_version=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
if [[ $(echo "$python_version < 3.7" | bc) -eq 1 ]]; then
    echo "Ошибка: Требуется Python 3.7 или выше!"
    echo "У вас установлен: Python $python_version"
    exit 1
fi

echo "✅ Python $python_version найден"

# Проверяем pip
if ! command -v pip3 &> /dev/null; then
    echo "Устанавливаем pip..."
    python3 -m ensurepip --upgrade
    if [ $? -ne 0 ]; then
        echo "Ошибка: Не удалось установить pip"
        exit 1
    fi
fi

# Устанавливаем зависимости
echo "Устанавливаем зависимости..."
pip3 install -r requirements.txt

if [ $? -ne 0 ]; then
    echo "Ошибка установки зависимостей!"
    exit 1
fi

echo ""
echo "========================================"
echo "Установка завершена успешно!"
echo "========================================"
echo ""
echo "Для запуска приложения:"
echo "1. Запустите сервер: python3 run.py"
echo "2. Откройте браузер: http://localhost:5000"
echo ""
read -p "Запустить приложение сейчас? (y/N): " -n 1 -r
echo ""
if [[ $REPLY =~ ^[YyДд]$ ]]; then
    echo "Запускаем приложение..."
    python3 run.py
fi