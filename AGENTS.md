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
