# 🚀 Инструкция по установке Git и развертыванию проекта

## ❗ Проблема: Git не установлен

В вашей системе не установлен Git. Это необходимо для отправки кода на GitHub.

## 📋 Установка Git

### Windows:
1. **Скачайте Git с официального сайта**: https://git-scm.com/download/win
2. **Запустите установщик** и следуйте инструкциям
3. **Перезапустите командную строку** после установки

### Проверка установки:
```bash
git --version
```

## 🔄 Пошаговая инструкция после установки Git

### Шаг 1: Откройте командную строку в папке проекта
```bash
cd "c:\Users\Work\Desktop\Проект VNC+print\work-w"
```

### Шаг 2: Инициализация Git репозитория
```bash
git init
```

### Шаг 3: Добавление файлов
```bash
git add .
```

### Шаг 4: Создание первого коммита
```bash
git commit -m "Initial commit: VNC and Printer Manager with Docker support

- Flask web application for managing VNC servers and printers
- noVNC support for browser-based connections
- Docker containerization
- REST API for device management
- Import/Export functionality
- Device grouping"
```

### Шаг 5: Создание репозитория на GitHub
1. Перейдите на https://github.com/new
2. Название: `vnc_print_manager`
3. Описание: `VNC & Printer Manager - Docker контейнер для управления VNC серверами и принтерами`
4. Сделайте репозиторий публичным
5. Не инициализируйте README, .gitignore или лицензию

### Шаг 6: Подключение к GitHub
```bash
git remote add origin https://github.com/ВАШ_USERNAME/vnc_print_manager.git
```
*Замените ВАШ_USERNAME на ваш никнейм в GitHub*

### Шаг 7: Отправка кода на GitHub
```bash
git branch -M main
git push -u origin main
```

## 🐳 Запуск Docker контейнера

После отправки на GitHub, запустите проект:

```bash
docker-compose up -d
```

Или через Docker:
```bash
docker build -t vnc-print-manager .
docker run -d --name vnc-print-manager -p 5000:5000 -p 6080:6080 vnc-print-manager
```

## 📱 Доступ к приложению

После запуска откройте в браузере:
- **Веб-интерфейс**: http://localhost:5000
- **noVNC**: порт 6080

## 🔧 Если возникли проблемы

### Проверка Docker:
```bash
docker --version
docker-compose --version
```

### Проверка портов:
```bash
netstat -an | findstr 5000
netstat -an | findstr 6080
```

### Просмотр логов:
```bash
docker logs vnc-print-manager
```

---

## ✅ Что уже готово:

- [x] Dockerfile оптимизирован для Python 3.11
- [x] docker-compose.yml готов к запуску
- [x] .gitignore создан
- [x] README_DOCKER.md с полной документацией
- [x] DEPLOYMENT_GUIDE.md с инструкциями
- [x] Все файлы проекта на месте

**Осталось только:**
1. Установить Git
2. Выполнить команды выше
3. Создать репозиторий на GitHub
4. Запустить Docker контейнер

Удачи! 🚀
