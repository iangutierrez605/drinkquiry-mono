# Production web image (Handoff #7 §H): node builds the SPA, the result is
# COPIED into the Caddy image (chosen over a shared volume: a volume would
# keep serving stale files after a rebuild unless manually cleared, while a
# copy makes `git pull && docker compose -f docker-compose.prod.yml up -d
# --build` ship every frontend change with nothing to remember).
#
# Build context is the REPO ROOT (see docker-compose.prod.yml) so the image
# can take both frontend/ and the root Caddyfile.

FROM node:22-alpine AS build
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
# Same-origin build (Handoff #5): no VITE_API_BASE — api.js derives relative
# URLs and Caddy routes /api, /ws, /admin, /static, /media alongside the SPA.
RUN npm run build

FROM caddy:2-alpine
COPY --from=build /app/dist /srv/app
COPY Caddyfile /etc/caddy/Caddyfile
