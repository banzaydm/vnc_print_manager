# VNC & Printer Manager 1.0.5

Docker-веб-приложение для управления сетью: VNC-серверы, принтеры, камеры, роутеры — единый интерфейс с мониторингом, сканированием, удалённым доступом и интеграциями RustDesk и Matrix.

## Возможности

### Устройства и организация
- **Четыре типа устройств**: VNC-серверы, сетевые принтеры, камеры (RTSP/HLS), роутеры.
- **Группы** с цветовыми метками и иерархией; **подсети** с именованными диапазонами; **избранное**.
- **Мульти-IP**: у сервера несколько адресов (основной + дополнительные) — онлайн, если доступен хотя бы один.
- **RustDesk ID** у сервера — подключение веб-клиентом RustDesk в один клик.

### Сканирование сети
- **Асинхронный скан** диапазонов: прогресс, статус, отмена, повторная проверка «живых, но не распознанных».
- Разведка **31 порта**, определение типа устройства по отпечатку (Proxmox, Pi-hole, OMV, OnlyOffice и др.).
- **Имя устройства цепочкой методов**: mDNS unicast → системный PTR → прямой DNS PTR → mDNS multicast → SSDP (friendlyName) → HTTP `<title>` (только 200) → SNMP sysName; слабые имена вида «Камера 1.2.3.4» автоматически улучшаются пост-проходом.
- **MAC-адреса** из ARP-таблицы; кнопка «Добавить все найденное».

### Доступ к устройствам
- **noVNC** — VNC прямо в браузере (WebSocket-прокси на порту 6080, одноразовые токены с TTL).
- **Веб-клиент RustDesk** — встроенная сессия с автоподключением по ID.
- **Стена камер** — мультиплексная раскладка камер с HLS-потоками (ffmpeg → hls.min.js).

### Вкладка RustDesk
- Список устройств из панели lejianwen/rustdesk-api, онлайн-статусы (опрос каждые 45 с), привязка устройств к серверам приложения, индикаторы статуса на кнопках «Подключиться».

### Вкладка Matrix (админка Synapse)
- Список пользователей homeserver: **онлайн** (по last_seen), время последнего визита, активность за 24 ч, роль, дата создания.
- **Создание пользователей** (пароль можно сгенерировать), **смена пароля**, **деактивация/включение**, выдача прав администратора.
- **Комнаты сервера** (участники, публичность), поиск по списку.
- Настройки: адрес homeserver, логин и пароль администратора (пароль не отдаётся клиенту).

### Безопасность и доступ
- **Авторизация** (`AUTH_ENABLED=1`): сессии, роли admin/user, управление пользователями, автосоздание первого админа из переменных окружения.
- **API-ключ** для внешних клиентов; **CSRF-защита** (проверка Origin); раздача статики закрыта от чувствительных файлов (`.py`, `.db`, `.git`, `instance/`).
- **SSL/TLS необязателен** — приложение рассчитано на работу за reverse proxy (Nginx Proxy Manager и т.п.).

### Настройки и данные
- **Темы** (светлая/тёмная), цвет акцента, название приложения, favicon и логотип.
- **Очистка базы выборочно**: серверы, принтеры, камеры, роутеры, группы — каждый тип отдельным чекбоксом.
- **Импорт/экспорт** данных (JSON), резервные копии в `instance/backups`.
- Вкладка «О приложении»: версия и дата обновления.

## Быстрый старт

```bash
git clone https://github.com/banzaydm/vnc_print_manager.git
cd vnc_print_manager
docker compose up -d --build
```

- Веб-интерфейс: http://localhost:5000
- noVNC прокси: порт 6080

Или без compose:

```bash
docker build -t vnc-print-manager .
docker run -d --name vnc-print-manager \
  -p 5000:5000 -p 6080:6080 \
  -e AUTH_ENABLED=1 \
  -v vnc_manager_data:/app/instance \
  vnc-print-manager
```

## Конфигурация (переменные окружения)

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `AUTH_ENABLED` | `0` | Включить авторизацию страниц и API |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | — | Первый администратор (создаётся при пустой базе) |
| `API_KEY` | — | Ключ для доступа к API внешними клиентами |
| `SECRET_KEY` | генерируется | Подпись сессий (хранится в `instance/secret_key`) |
| `INIT_DB_ON_START` | — | Инициализация БД при старте |
| `NOVNC_PROXY_PORT` | `6080` | Порт WebSocket-прокси noVNC |
| `NOVNC_TOKEN_TTL_SECONDS` | `600` | Время жизни токена noVNC |
| `START_WEBSOCKIFY` | `1` | Запуск websockify внутри контейнера |
| `FLASK_APP` / `FLASK_ENV` | `app.py` / `production` | Параметры Flask |

Тома: `vnc_manager_data` → `/app/instance` (БД, токены, бэкапы), `vnc_uploads_data` → `/app/uploads` (логотип/favicon).

## Структура проекта

```
├── app.py               # Flask-приложение, гейты авторизации и статики
├── models.py            # Модели БД (SQLite)
├── database.py          # Инициализация БД
├── run.py               # Точка входа (Flask + websockify)
├── index.html           # SPA веб-интерфейс (инлайн-стили и скрипты)
├── blueprints/          # Маршруты: auth, data, devices, streams, scan,
│                        #   rustdesk, matrix, vnc, settings_api
├── services/            # Логика: скан (scan_runner/scan_service), статусы,
│                        #   импорт/экспорт, настройки, Matrix-клиент, RustDesk API
├── rustdesk_web/        # Веб-клиент RustDesk
├── backups/             # Бэкапы проекта (вне контейнера)
├── Dockerfile
├── docker-compose.yml
└── instance/            # vnc_manager.db, секреты, токены, бэкапы БД
```

## API (обзор)

- **Данные**: `GET/POST /api/servers|printers|cameras|routers|groups`, `PUT/DELETE /api/<тип>/<id>`, `/api/subnets`
- **Скан**: `POST /api/scan/start`, `GET /api/scan/status`, `POST /api/scan/cancel`
- **Доступ**: `POST /api/connect/<id>`, `POST /api/novnc/<id>/token`
- **RustDesk**: `GET /api/rustdesk/status`, `GET /api/rustdesk/devices`, `POST /api/rustdesk/add`
- **Matrix**: `GET /api/matrix/status|users|rooms`, `POST /api/matrix/users`, `POST /api/matrix/users/update`
- **Система**: `GET/POST /api/settings`, `GET/POST /api/users` (админ), `POST /api/admin/clear_db`, `GET /api/export`, `POST /api/import`, `GET /api/config`

Все `/api/*` требуют авторизацию (сессия или `X-API-Key`), если включён `AUTH_ENABLED`.

## Траблшутинг

- **VNC не подключается** — проверьте доступность `ping <IP>` и порт `telnet <IP> 5900`; убедитесь, что VNC-сервер запущен и файрвол не блокирует.
- **noVNC не открывается** — порт 6080 должен быть доступен; логи: `docker logs <контейнер>`.
- **Скан не находит устройства** — убедитесь, что контейнер в той же L2-сети; имена берутся из mDNS/PTR/SSDP/SNMP — сеть должна отвечать на эти протоколы.
- **Matrix не грузится** — проверьте адрес homeserver и логин/пароль администратора в настройках; Synapse ratelimit'ит частые логины с одного IP.
- **БД** — SQLite-файл `instance/vnc_manager.db`; том должен быть смонтирован, иначе данные теряются при пересборке.

## Безопасность

1. Включите `AUTH_ENABLED=1` и задайте сильный `ADMIN_PASSWORD`.
2. Выставьте `SECRET_KEY` и `API_KEY`.
3. Обращайте приложение за HTTPS reverse proxy.
4. Регулярно делайте копии тома `vnc_manager_data`.

## Лицензия

MIT License
