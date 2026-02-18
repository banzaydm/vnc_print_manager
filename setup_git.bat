@echo off
chcp 65001 >nul

echo === VNC Print Manager - Git Setup ===

REM Проверка наличия Git
git --version >nul 2>&1
if %errorlevel% neq 0 (
    echo ❌ Git не установлен. Пожалуйста установите Git:
    echo    Windows: https://git-scm.com/download/win
    pause
    exit /b 1
)

REM Инициализация репозитория
echo 📦 Инициализация Git репозитория...
git init

REM Добавление файлов в .gitignore
echo 📝 Создание .gitignore...
(
echo # Бинарные файлы
echo __pycache__/
echo *.py[cod]
echo *$py.class
echo.
echo # База данных
echo *.db
echo *.sqlite
echo *.sqlite3
echo.
echo # Логи
echo *.log
echo instance/*.log
echo.
echo # Виртуальное окружение
echo venv/
echo env/
echo ENV/
echo.
echo # IDE
echo .vscode/
echo .idea/
echo *.swp
echo *.swo
echo.
echo # OS
echo .DS_Store
echo Thumbs.db
echo.
echo # Docker
echo .dockerignore
echo.
echo # Временные файлы
echo *.tmp
echo *.temp
) > .gitignore

REM Добавление всех файлов
echo ➕ Добавление файлов в репозиторий...
git add .

REM Первый коммит
echo 💾 Создание первого коммита...
git commit -m "Initial commit: VNC ^& Printer Manager with Docker support

- Flask веб-приложение для управления VNC серверами и принтерами
- Поддержка noVNC для подключения через браузер
- Docker контейнеризация
- REST API для управления устройствами
- Импорт/Экспорт данных
- Группировка устройств"

echo.
echo === Готово! ===
echo.
echo 🚀 Теперь выполните следующие команды для отправки в GitHub:
echo.
echo 1. Создайте новый репозиторий на GitHub с названием 'vnc_print_manager'
echo    https://github.com/new
echo.
echo 2. Добавьте удаленный репозиторий:
echo    git remote add origin https://github.com/ВАШ_USERNAME/vnc_print_manager.git
echo.
echo 3. Отправьте код в GitHub:
echo    git branch -M main
echo    git push -u origin main
echo.
echo 📝 Замените ВАШ_USERNAME на ваш никнейм в GitHub
echo.
echo 🐳 После этого вы сможете запустить проект через Docker:
echo    docker-compose up -d
echo.
pause
