@echo off
echo ========================================
echo VNC & Printer Manager - Установка
echo ========================================
echo.

REM Проверяем Python
python --version >nul 2>&1
if errorlevel 1 (
    echo Ошибка: Python не найден!
    echo Установите Python 3.7 или выше с сайта:
    echo https://www.python.org/downloads/
    pause
    exit /b 1
)

REM Устанавливаем зависимости
echo Устанавливаем зависимости...
pip install -r requirements.txt

if errorlevel 1 (
    echo Ошибка установки зависимостей!
    pause
    exit /b 1
)

echo.
echo ========================================
echo Установка завершена успешно!
echo ========================================
echo.
echo Для запуска приложения:
echo 1. Запустите сервер: python run.py
echo 2. Откройте браузер: http://localhost:5000
echo.
pause