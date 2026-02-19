FROM python:3.11-slim

# Устанавливаем рабочую директорию
WORKDIR /app

# Копируем файлы зависимостей и устанавливаем их
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

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
