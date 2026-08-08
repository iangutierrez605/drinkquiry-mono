FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 libmagic1 \
    # §F5 (#21): the crib-sheet PDF's ⚡ needs a Unicode font — slim ships
    # none. games/pdf.py registers DejaVu when present and falls back to a
    # text marker when not, so this line is belt AND suspenders.
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

EXPOSE 8000

# Production entrypoint (docker-compose overrides this with runserver for dev).
# Daphne serves both HTTP and WebSockets from the ASGI app.
CMD ["daphne", "-b", "0.0.0.0", "-p", "8000", "config.asgi:application"]
