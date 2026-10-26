FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 APP_ENV=production
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py ./
COPY static ./static
RUN useradd --system --uid 10001 app && chown -R app /app
USER app
EXPOSE 10000
# Exactly ONE instance must run: the in-memory workspace is not shared between processes.
CMD ["python", "app.py"]
