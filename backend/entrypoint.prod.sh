#!/bin/sh
# Production entrypoint (Handoff #7 §H) — used by docker-compose.prod.yml.
# The git-pull update workflow relies on this: every `up -d --build` migrates
# and re-collects static before daphne starts, so a deploy is one command.
set -e

python manage.py migrate --noinput
python manage.py collectstatic --noinput

# Daphne serves HTTP and WebSockets from the one ASGI app; Caddy proxies
# /api, /ws and /admin here and serves /static (collected above into the
# shared staticfiles volume) and the SPA itself.
exec daphne -b 0.0.0.0 -p 8000 config.asgi:application
