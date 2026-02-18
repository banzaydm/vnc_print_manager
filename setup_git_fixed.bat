@echo off
echo === VNC Print Manager - Git Setup ===

REM Проверка наличия Git
git --version >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: Git is not installed. Please install Git:
    echo    Windows: https://git-scm.com/download/win
    pause
    exit /b 1
)

REM Инициализация репозитория
echo [+] Initializing Git repository...
git init

REM Добавление файлов в .gitignore
echo [+] Creating .gitignore...
(
echo # Binary files
echo __pycache__/
echo *.py[cod]
echo *$py.class
echo.
echo # Database
echo *.db
echo *.sqlite
echo *.sqlite3
echo.
echo # Logs
echo *.log
echo instance/*.log
echo.
echo # Virtual environment
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
echo # Temp files
echo *.tmp
echo *.temp
) > .gitignore

REM Добавление всех файлов
echo [+] Adding files to repository...
git add .

REM Первый коммит
echo [+] Creating first commit...
git commit -m "Initial commit: VNC and Printer Manager with Docker support

- Flask web application for managing VNC servers and printers
- noVNC support for browser-based connections
- Docker containerization
- REST API for device management
- Import/Export functionality
- Device grouping"

echo.
echo === Done! ===
echo.
echo Now run these commands to push to GitHub:
echo.
echo 1. Create a new repository on GitHub named 'vnc_print_manager'
echo    https://github.com/new
echo.
echo 2. Add remote repository:
echo    git remote add origin https://github.com/YOUR_USERNAME/vnc_print_manager.git
echo.
echo 3. Push to GitHub:
echo    git branch -M main
echo    git push -u origin main
echo.
echo Replace YOUR_USERNAME with your GitHub username
echo.
echo After that you can run the project via Docker:
echo    docker-compose up -d
echo.
pause
