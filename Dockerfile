FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Apply database migrations, then start the web server on the port Railway assigns.
CMD ["sh", "-c", "alembic upgrade head && uvicorn kria.web.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
