FROM python:3.11-slim

# Устанавливаем рабочую директорию
WORKDIR /app

# Копируем файлы зависимостей и устанавливаем их.
# Индекс PyPI настраивается через --build-arg PIP_INDEX_URL (например,
# --build-arg PIP_INDEX_URL=https://pypi.org/simple), чтобы обойти блокировки pypi.org.
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
COPY requirements.txt .
RUN pip install --no-cache-dir --timeout 60 --retries 5 -r requirements.txt -i $PIP_INDEX_URL

# FFmpeg ставится через pip (imageio-ffmpeg, статический бинарь с libx264),
# чтобы не зависеть от apt-репозиториев Debian (на некоторых серверах
# DNS-резолвер apt не работает, apt-get install ffmpeg падает с exit 100).

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
