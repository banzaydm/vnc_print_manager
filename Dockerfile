FROM python:3.11-slim

# Устанавливаем рабочую директорию
WORKDIR /app

# Копируем файлы зависимостей и устанавливаем их.
# Индекс PyPI настраивается через --build-arg PIP_INDEX_URL (например,
# --build-arg PIP_INDEX_URL=https://pypi.org/simple), чтобы обойти блокировки pypi.org.
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
COPY requirements.txt .
RUN pip install --no-cache-dir --timeout 60 --retries 5 -r requirements.txt -i $PIP_INDEX_URL

# FFmpeg для перекодировки RTSP в HLS (просмотр камер в браузере).
# Зеркало APT задаётся ХОСТОМ через --build-arg APT_MIRROR (например,
# --build-arg APT_MIRROR=deb.debian.org) — пути /debian и /debian-security
# сохраняются. По умолчанию — зеркало Tsinghua (обходит блокировку deb.debian.org).
ARG APT_MIRROR=mirrors.tuna.tsinghua.edu.cn
RUN sed -i "s|deb.debian.org|$APT_MIRROR|g; s|security.debian.org|$APT_MIRROR|g" /etc/apt/sources.list.d/debian.sources 2>/dev/null; \
    sed -i "s|deb.debian.org|$APT_MIRROR|g; s|security.debian.org|$APT_MIRROR|g" /etc/apt/sources.list 2>/dev/null; \
    echo "APT_MIRROR=$APT_MIRROR"; \
    apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

# Копируем все файлы приложения
COPY . .

# Создаем директорию для instance (база данных и логи)
RUN mkdir -p instance

# Устанавливаем переменные окружения
ENV FLASK_APP=app.py
ENV FLASK_ENV=production
ENV NOVNC_PROXY_PORT=6080
ENV NOVNC_TOKEN_TTL_SECONDS=600

# Открываем порты
EXPOSE 5000 6080

# Команда для запуска приложения
CMD ["python", "run.py"]
