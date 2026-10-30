# Тестовый VNC-сервер для проверки буфера обмена noVNC.
# Xvfb + fluxbox + xterm + x11vnc (legacy RFB) + xclip (чтение/запись CLIPBOARD).
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb \
        x11vnc \
        fluxbox \
        xterm \
        xclip \
        x11-utils \
        x11-xserver-utils \
        dbus-x11 \
    && rm -rf /var/lib/apt/lists/*

COPY start.sh /start.sh
COPY Xresources /Xresources
# Защита от CRLF при сборке на Windows
RUN sed -i 's/\r$//' /start.sh && chmod +x /start.sh

ENV VNC_PASSWORD=testvnc

EXPOSE 5901

CMD ["/start.sh"]
