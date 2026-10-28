# Правила работы с проектом (VNC Print Manager)

## Бэкап перед каждой правкой — обязательно

Перед **каждым** внесением изменений в файлы проекта (код, конфиги, HTML/CSS,
Dockerfile, документация) выполнять бэкап всего проекта:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "C:\Users\Work\Desktop\VNC_mng\backup-project.ps1" -Note "<что буду менять>"
```

- Копия создаётся в `C:\Users\Work\Desktop\VNC_mng\backups\work-w_<дата>_<время>\`
  вместе с `manifest.txt` (HEAD коммит, `git status`, комментарий).
- Копируются **всё**, включая `.git` и БД `vnc_manager.db` (откат «целиком»);
  исключаются только `venv/` и `__pycache__` (воссоздаются из requirements.txt).
- Хранятся последние 30 бэкапов, старые удаляются автоматически.
- Один бэкап покрывает серию правок одной задачи; новый — перед следующей задачей
  или если предыдущая задача закоммичена/завершена.

### Откат

```powershell
# посмотреть бэкапы
Get-ChildItem C:\Users\Work\Desktop\VNC_mng\backups -Directory
# откат: удалить проект и распаковать бэкап (после остановки контейнера)
```

## Сборка и деплой

- Локальная проверка: `docker compose up -d --build` (докер локально работает).
- После каждой правки кода — пересобрать контейнер; прод собирается в Portainer
  (`http://192.168.17.250:9000`, стек `vnc-print-manager`), коммит/пуш — только
  по явной просьбе пользователя.
- Проверка синтаксиса перед завершением: `python -m py_compile app.py`
  и `node --check` для `<script>`-блока `index.html`.

## Устройство

- Монолит `app.py` (~3900 строк), фронтенд — `index.html` (стили в инлайн-`<style>`,
  файл `style.css` в работу не подключается).
- RustDesk: `Server.rustdesk_id`, панель lejianwen/rustdesk-api, статусы —
  `GET /api/rustdesk/status` (кэш 30 с, индекс по ID и по IP).

## Matrix (настроено и работает, состояние на 10.2026)

Вкладка **Matrix** — админка нашего Synapse; **с настройкой сервера закончили,
всё устраивает** — при доработках от этой базы не отступать.

### Сервер

- Synapse 1.162.0 на `192.168.17.250:8008` (docker на omv), server_name `x.dmydm.ru`.
- Element-Web: `x.dmydm.ru → 192.168.17.250:8082` за NPM (`192.168.17.130:81`).
- Домен/SSL/прокси — через NPM; `/.well-known/matrix/client` → `https://x.dmydm.ru/`.
- TURN: `turn.dmydm.ru → 192.168.17.51:3478` (coturn). Для звонков на Keenetic
  пробрасывать: UDP 3478, TCP 3478, UDP 49152–65535 (relay range coturn) прямо
  на .51, **минуя NPM** (NPM не умеет UDP); опционально 5349 TCP (turns) и
  UDP 443 (DTLS), если coturn их слушает.
- Synapse раздаёт клиентам TURN: `GET /_matrix/client/v3/voip/turnServer` →
  `turn:x.dmydm.ru:3478?transport=udp|tcp`.

### Код в приложении

- `services/matrix_service.py` — клиент Synapse admin API: логин admin (токен
  кэш 12 ч), `list_users` (онлайн по `last_seen_ts`, окно 180 с), `list_rooms`,
  `create_user`/`update_user`, `send_broadcast` (DM через m.direct + общий рум
  «Рассылка VNC Manager»).
- `services/matrix_groups.py` — локальные данные вкладки (НЕ на сервере):
  группы (`MatrixGroup`/`MatrixGroupMember`) и скрытие (`MatrixHiddenUser`).
- `blueprints/matrix.py` — роуты `/api/matrix/*` (status, users, rooms,
  broadcast, groups CRUD+assign, hidden, users/hide).
- Настройки в Настройках приложения: `matrix_hs_url`, `matrix_admin_user`,
  `matrix_admin_pass` (маскируется в `/api/settings`).

### Особенности Synapse (важно, проверено на нашем сервере)

- **`POST .../send/...` отдаёт 405 M_UNRECOGNIZED — отправлять только `PUT`**
  (каноничный метод, его же использует Element). Та же фигня с `state`.
- **Удаление пользователей невозможно**: `DELETE /_synapse/admin/v2/users/*`
  → 405, `erase`/`deactivate` (v1) → 404. Поэтому «удалить» в UI = локальное
  скрытие (`POST /api/matrix/users/hide`), возврат — туда же `hidden: false`.
- **`invited_members` → 404** — состав приглашённых брать из
  `GET /rooms/{id}/members?membership=invite`.
- **Список `/_synapse/admin/v2/users` скрывает deactivated** — всегда добавлять
  `&deactivated=true&guests=true`, иначе отключённые не видны.
- Создание/обновление юзера — `PUT /_synapse/admin/v2/users/{id}` (upsert;
  POST → 405). Detail — GET того же пути.
- **РателIMIT логина ~3 попытки/5 мин на IP** — между живыми прогонами E2E
  выдерживать паузу ≥5 мин, гонять строго по одному прогону.
- Presence API недоступен (403) — онлайн только по `last_seen_ts`.
- **Вход админа в invite-only комнату**: прямой `POST /_synapse/admin/v1/join/{id}`
  → 403 «not in room» (инвайт шлётся от админа вне комнаты). Рабочая цепочка
  (`join_room()`): клиентский `POST /rooms/{id}/join` → при 403
  `POST /_synapse/admin/v1/rooms/{id}/make_room_admin` {user_id} (200:
  инвайт + power level) → повторный клиентский join (200). Комната с
  `!id` **без домена** в admin API; `!id:домен` → «Can't join remote room».
- **Состав участников без membership**: `GET /_synapse/admin/v1/rooms/{id}/members`
  → 200 (только joined, без ролей/invites); профили — `GET /_synapse/admin/v2/users/{uid}`.
- Админ state-эндпоинты (`/admin/v1/rooms/{id}/state/...`) → 404 — state
  только через client API после входа в комнату.

### Проверки

- Смоук `%TEMP%\opencode\matrix_smoke.py` (сеть мокается подменой
  `matrix_service._http`) — **131/131**: пользователи, комнаты, broadcast,
  группы, скрытие, маскировка пароля, fallback состава, join-цепочка.
- Живой E2E `%TEMP%\opencode\_matrix_e2e.py` — создание/DM/рум/очистка;
  гонять **в одиночку** (не параллельно со смоуками: они восстанавливают БД
  и перетирают креды Matrix под ногами).
- Регресс: routes baseline **73**, `auth 14`, `users 6`, `devices 16`,
  `multiip 12`, `scan 108`, `cleardb 4` — всё зелёное перед коммитом.
