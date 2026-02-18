## Настройка VNC клиентов

### Поддерживаемые VNC клиенты

#### macOS
- ✅ RealVNC Viewer
- ✅ TightVNC
- ✅ TigerVNC
- ✅ Chicken VNC
- ✅ JollysFastVNC
- ✅ Screens
- ✅ Встроенный Screen Sharing

#### Windows
- ✅ TightVNC
- ✅ RealVNC Viewer
- ✅ TigerVNC
- ✅ UltraVNC
- ✅ Любой клиент в PATH

#### Linux
- ✅ TightVNC (xtightvncviewer)
- ✅ TigerVNC (xtigervncviewer)
- ✅ Vinagre
- ✅ Remmina
- ✅ KRDC

### Если клиент не найден

1. **Установите TightVNC**:
   - Windows: http://www.tightvnc.com/download.php
   - macOS: `brew install --cask tightvnc`
   - Linux: `sudo apt install xtightvncviewer`

2. **Или добавьте путь к клиенту в PATH**

3. **Или используйте ручное подключение**:
   - Формат: `IP_адрес:порт` (например: `192.168.1.100:5900`)

### Тестирование VNC клиента
```bash
python test_vnc.py

## Подключение через noVNC (в браузере)

В интерфейсе рядом с кнопкой «Подключиться» есть кнопка **noVNC** — она открывает VNC-сессию прямо в браузере.

- **Прокси-порт**: по умолчанию используется `6080` (WebSocket прокси `websockify`).
- **Требования**: порт `6080` должен быть доступен с того устройства, где открыт браузер (если подключаетесь не с localhost — откройте порт в фаерволе).
- **Настройка** (опционально через переменные окружения):
  - `NOVNC_PROXY_PORT` — порт websockify (по умолчанию 6080)
  - `NOVNC_TOKEN_TTL_SECONDS` — время жизни токена (по умолчанию 600 секунд)# vnc_print_manager
