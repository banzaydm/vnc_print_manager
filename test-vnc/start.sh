#!/bin/sh
# Тестовый VNC: Xvfb :99 + fluxbox + xterm, x11vnc слушает 5901.
set -e

export DISPLAY=:99

Xvfb :99 -screen 0 1280x800x24 -ac +extension GLX +render -noreset &
XVFB_PID=$!

# Ждём, пока X поднимется
i=0
while [ $i -lt 50 ]; do
    xdpyinfo >/dev/null 2>&1 && break
    i=$((i + 1))
    sleep 0.2
done

# Оконный менеджер и терминал (для наглядной картинки)
fluxbox >/dev/null 2>&1 &
sleep 1
# Ctrl+V в xterm = вставка из CLIPBOARD. В Debian xterm по умолчанию Ctrl+V —
# это quoted-insert шелла (не вставка), поэтому задаём трансляцию явно.
# \n между биндингами обязателен: обычный перенос строки в Xresources склеивается.
if [ -f /Xresources ]; then
    xrdb -load /Xresources
fi
xterm -geometry 120x30+20+20 -title "Clipboard test" &

# x11vnc в фореграунде, чтобы контейнер не падал
exec x11vnc \
    -display :99 \
    -forever \
    -shared \
    -rfbport 5901 \
    -passwd "${VNC_PASSWORD:-testvnc}" \
    -rfbversion 3.8 \
    -quiet
