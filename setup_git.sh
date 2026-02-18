#!/bin/bash

# Скрипт для инициализации Git репозитория и отправки в GitHub

echo "=== VNC Print Manager - Git Setup ==="

# Проверка наличия Git
if ! command -v git &> /dev/null; then
    echo "❌ Git не установлен. Пожалуйста установите Git:"
    echo "   Windows: https://git-scm.com/download/win"
    echo "   macOS: brew install git"
    echo "   Linux: sudo apt install git"
    exit 1
fi

# Инициализация репозитория
echo "📦 Инициализация Git репозитория..."
git init

# Добавление файлов в .gitignore
echo "📝 Создание .gitignore..."
cat > .gitignore << 'EOF'
# Бинарные файлы
__pycache__/
*.py[cod]
*$py.class

# База данных
*.db
*.sqlite
*.sqlite3

# Логи
*.log
instance/*.log

# Виртуальное окружение
venv/
env/
ENV/

# IDE
.vscode/
.idea/
*.swp
*.swo

# OS
.DS_Store
Thumbs.db

# Docker
.dockerignore

# Временные файлы
*.tmp
*.temp
EOF

# Добавление всех файлов
echo "➕ Добавление файлов в репозиторий..."
git add .

# Первый коммит
echo "💾 Создание первого коммита..."
git commit -m "Initial commit: VNC & Printer Manager with Docker support

- Flask веб-приложение для управления VNC серверами и принтерами
- Поддержка noVNC для подключения через браузер
- Docker контейнеризация
- REST API для управления устройствами
- Импорт/Экспорт данных
- Группировка устройств"

# Инструкции для GitHub
echo ""
echo "=== Готово! ==="
echo ""
echo "🚀 Теперь выполните следующие команды для отправки в GitHub:"
echo ""
echo "1. Создайте новый репозиторий на GitHub с названием 'vnc_print_manager'"
echo "   https://github.com/new"
echo ""
echo "2. Добавьте удаленный репозиторий:"
echo "   git remote add origin https://github.com/ВАШ_USERNAME/vnc_print_manager.git"
echo ""
echo "3. Отправьте код в GitHub:"
echo "   git branch -M main"
echo "   git push -u origin main"
echo ""
echo "📝 Замените ВАШ_USERNAME на ваш никнейм в GitHub"
echo ""
echo "🐳 После этого вы сможете запустить проект через Docker:"
echo "   docker-compose up -d"
echo ""
