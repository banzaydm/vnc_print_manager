# 🐳 Инструкция по развертыванию VNC Print Manager

## 📋 Что было сделано

Проект **VNC & Printer Manager** успешно подготовлен для развертывания в Docker контейнере и публикации на GitHub.

### 🎯 Созданные файлы:

1. **Dockerfile** - конфигурация Docker контейнера
2. **docker-compose.yml** - оркестрация контейнеров
3. **.dockerignore** - исключение ненужных файлов из образа
4. **README_DOCKER.md** - полная документация проекта
5. **setup_git.sh** / **setup_git.bat** - скрипты для инициализации Git

---

## 🚀 Пошаговая инструкция по запуску

### Шаг 1: Подготовка GitHub репозитория

1. **Запустите скрипт инициализации Git**:
   ```bash
   # Для Windows:
   setup_git.bat
   
   # Для Linux/macOS:
   chmod +x setup_git.sh
   ./setup_git.sh
   ```

2. **Создайте репозиторий на GitHub**:
   - Перейдите на https://github.com/new
   - Название репозитория: `vnc_print_manager`
   - Описание: `VNC & Printer Manager - Docker контейнер для управления VNC серверами и принтерами`
   - Сделайте репозиторий публичным
   - Не инициализируйте README, .gitignore или лицензию

3. **Отправьте код в GitHub**:
   ```bash
   git remote add origin https://github.com/ВАШ_USERNAME/vnc_print_manager.git
   git branch -M main
   git push -u origin main
   ```

### Шаг 2: Запуск через Docker Compose (рекомендуется)

1. **Перейдите в папку проекта**:
   ```bash
   cd "c:/Users/Work/Desktop/Проект VNC+print/work-w"
   ```

2. **Запустите контейнер**:
   ```bash
   docker-compose up -d
   ```

3. **Проверьте статус**:
   ```bash
   docker-compose ps
   ```

4. **Откройте приложение**:
   - Веб-интерфейс: http://localhost:5000
   - Логи: `docker-compose logs -f`

### Шаг 3: Запуск через Docker (альтернативный способ)

1. **Соберите образ**:
   ```bash
   docker build -t vnc-print-manager .
   ```

2. **Запустите контейнер**:
   ```bash
   docker run -d \
     --name vnc-print-manager \
     -p 5000:5000 \
     -p 6080:6080 \
     -v "$(pwd)/instance:/app/instance" \
     vnc-print-manager
   ```

---

## 🔧 Конфигурация и настройка

### Основные порты:
- **5000** - Веб-интерфейс Flask приложения
- **6080** - noVNC WebSocket прокси

### Переменные окружения:
```bash
FLASK_APP=app.py                    # Файл приложения
FLASK_ENV=production                # Режим работы
NOVNC_PROXY_PORT=6080              # Порт noVNC прокси
NOVNC_TOKEN_TTL_SECONDS=600        # Время жизни токена (10 минут)
```

### Хранение данных:
- **База данных**: `vnc_manager.db` (SQLite)
- **Логи**: `instance/` директория
- **Временные файлы**: `instance/` директория

---

## 📱 Использование приложения

### Основные функции:

1. **Управление VNC серверами**:
   - Добавление серверов через веб-интерфейс
   - Проверка статуса доступности
   - Подключение через нативный VNC клиент
   - Подключение через noVNC (в браузере)

2. **Управление принтерами**:
   - Добавление сетевых принтеров
   - Мониторинг доступности
   - Быстрый доступ к веб-интерфейсу принтеров

3. **Группировка**:
   - Создание групп для организации устройств
   - Цветовая маркировка групп
   - Иерархическая структура групп

4. **Дополнительные функции**:
   - Экспорт/импорт конфигурации
   - Избранные серверы
   - Комментарии к устройствам

### Пример использования:

1. **Добавьте VNC сервер**:
   - IP: `192.168.1.100`
   - Порт: `5900`
   - Название: `Сервер отдела`

2. **Подключитесь к серверу**:
   - Нажмите "Подключиться" для нативного клиента
   - Нажмите "noVNC" для подключения в браузере

3. **Добавьте принтер**:
   - IP: `192.168.1.50`
   - Веб-интерфейс: `http://192.168.1.50`
   - Название: `Принтер HP`

---

## 🔍 Траблшутинг

### Проблемы и решения:

1. **Контейнер не запускается**:
   ```bash
   # Проверьте логи
   docker logs vnc-print-manager
   
   # Проверьте занятость портов
   netstat -an | findstr 5000
   netstat -an | findstr 6080
   ```

2. **VNC подключение не работает**:
   - Проверьте доступность сервера: `ping 192.168.1.100`
   - Проверьте порт VNC: `telnet 192.168.1.100 5900`
   - Убедитесь что VNC сервер запущен на удаленной машине

3. **noVNC не работает**:
   - Проверьте порт 6080: `netstat -an | findstr 6080`
   - Проверьте логи websockify в контейнере
   - Убедитесь что файрвол блокирует порт 6080

4. **База данных не сохраняется**:
   - Проверьте монтирование томов: `docker inspect vnc-print-manager`
   - Убедитесь что директория `instance/` существует

---

## 🔒 Безопасность в продакшене

### Рекомендации:

1. **Измените порты по умолчанию**:
   ```yaml
   ports:
     - "8080:5000"  # Вместо 5000
     - "8081:6080"  # Вместо 6080
   ```

2. **Добавьте reverse proxy (nginx)**:
   ```nginx
   server {
       listen 80;
       server_name your-domain.com;
       
       location / {
           proxy_pass http://localhost:8080;
       }
       
       location /novnc/ {
           proxy_pass http://localhost:8081;
           proxy_http_version 1.1;
           proxy_set_header Upgrade $http_upgrade;
           proxy_set_header Connection "upgrade";
       }
   }
   ```

3. **Ограничьте доступ по IP**:
   ```yaml
   ports:
     - "127.0.0.1:5000:5000"  # Только локальный доступ
   ```

4. **Регулярные бэкапы**:
   ```bash
   # Бэкап базы данных
   docker exec vnc-print-manager cp /app/vnc_manager.db /backup/
   docker cp vnc-print-manager:/backup/vnc_manager.db ./backup_$(date +%Y%m%d).db
   ```

---

## 📚 Полезные команды

### Docker команды:
```bash
# Просмотр логов
docker logs -f vnc-print-manager

# Перезапуск контейнера
docker-compose restart

# Остановка и удаление
docker-compose down

# Обновление приложения
git pull
docker-compose up -d --build
```

### Мониторинг:
```bash
# Статус контейнера
docker ps

# Использование ресурсов
docker stats vnc-print-manager

# Вход в контейнер
docker exec -it vnc-print-manager bash
```

---

## 🎉 Готово!

Теперь у вас есть полностью готовый Docker контейнер **VNC Print Manager**, который:

✅ Управляет VNC серверами  
✅ Мониторит принтеры  
✅ Предоставляет веб-интерфейс  
✅ Поддерживает noVNC  
✅ Готов к продакшену  
✅ Опубликован на GitHub  

Приложение доступно по адресу **http://localhost:5000** после запуска!
