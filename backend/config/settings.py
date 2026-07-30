"""
Drinkquiry settings — Django 6.0, environment-driven.
"""
import os
from pathlib import Path

import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent


def env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def env_list(name: str, default: str = "") -> list[str]:
    raw = os.environ.get(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-insecure-key")
DEBUG = env_bool("DJANGO_DEBUG", True)

# --- Hosts / CORS / CSRF: environment-driven, no hardcoded IPs (Handoff #5) --
# Dev (DEBUG=true): defaults are permissive — any Host header is accepted and
# any Origin gets CORS headers — so hitting the API from a phone on the LAN
# (http://<your-ip>:5173 → http://<your-ip>:8000) just works with no config.
# Prod (DEBUG=false): defaults are empty and DJANGO_ALLOWED_HOSTS +
# CORS_ALLOWED_ORIGINS (or a same-origin reverse proxy) MUST be set in the
# environment. Note: AllowedHostsOriginValidator vets WebSocket Origins
# against ALLOWED_HOSTS, so if the SPA is served from a different hostname
# than the API, list BOTH hostnames in DJANGO_ALLOWED_HOSTS.
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "*" if DEBUG else "")

INSTALLED_APPS = [
    "daphne",  # must precede staticfiles so runserver uses ASGI
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # third party
    "rest_framework",
    "knox",
    "corsheaders",
    "channels",
    # local
    "accounts",
    "trivia",
    "games",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

DATABASES = {
    "default": dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        conn_max_age=600,
    )
}

REDIS_URL = os.environ.get("REDIS_URL")
if REDIS_URL:
    CHANNEL_LAYERS = {
        "default": {
            "BACKEND": "channels_redis.core.RedisChannelLayer",
            "CONFIG": {"hosts": [REDIS_URL]},
        }
    }
else:
    # Local dev without Redis (single process only)
    CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}

# --- Cache (§I1, Handoff #10) -----------------------------------------------
# Django 6 ships a native RedisCache — NO new dependency. When REDIS_URL is
# set (compose/prod), the default cache shares the channel layer's Redis
# instance with a "cache" key prefix to stay out of its way; otherwise LocMem
# (dev + the SQLite-fallback suite, unchanged). This quietly FIXES a latent
# #9 issue: the forgot-password cooldown was LocMem = per-process; on Redis
# it (and §I2's throttle counters) is global across daphne processes.
if REDIS_URL:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": REDIS_URL,
            "KEY_PREFIX": "cache",
        }
    }
else:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ("knox.auth.TokenAuthentication",),
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 50,
    # §I2 (Handoff #10): per-IP rates for the public auth surface, applied
    # PER-VIEW via accounts/throttling.py — deliberately NOT a global default
    # (game join and the polling snapshot must stay unthrottled; a bar full
    # of phones behind one NAT IP is the normal case, not an attack).
    # Settings constants, not env — env-overridable would be overkill.
    "DEFAULT_THROTTLE_RATES": {
        "login": "10/min",
        "register": "5/min",
        "password_forgot": "5/min",
        "password_reset": "5/min",
    },
}

# In dev every origin is allowed (CORS_ALLOW_ALL_ORIGINS below), so the
# explicit list only matters in production. Full scheme://host[:port] entries,
# comma-separated, e.g. "https://app.example.com".
CORS_ALLOWED_ORIGINS = env_list("CORS_ALLOWED_ORIGINS", "")
CORS_ALLOW_ALL_ORIGINS = env_bool("CORS_ALLOW_ALL_ORIGINS", DEBUG)  # permissive in dev only
# Only needed for cross-origin cookie/session posts (the SPA uses Knox token
# auth, which is CSRF-exempt) — in practice: the /admin site behind a proxy.
CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS", "")

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

# --- Media storage: local disk or S3/DigitalOcean Spaces ---
# Credentials are wired EXPLICITLY into OPTIONS (Handoff #7 §F1) rather than
# left to boto3's implicit env/instance-profile discovery, so exactly one pair
# of env vars decides what is used. django-storages' S3Storage reads
# `access_key`/`secret_key` from OPTIONS first; we accept both the AWS_S3_*
# names (storages' own aliases) and the plain AWS_* names boto3 uses.
if os.environ.get("MEDIA_BACKEND", "local") == "s3":
    STORAGES = {
        "default": {
            "BACKEND": "storages.backends.s3.S3Storage",
            "OPTIONS": {
                "bucket_name": os.environ.get("AWS_STORAGE_BUCKET_NAME", ""),
                # Optional key prefix inside the bucket (e.g. "drinkquiry" to
                # share a bucket: <bucket>/drinkquiry/questions/images/...).
                # Empty = bucket root. Signed URLs include it automatically.
                "location": os.environ.get("AWS_S3_LOCATION", ""),
                "endpoint_url": os.environ.get("AWS_S3_ENDPOINT_URL") or None,
                "region_name": os.environ.get("AWS_S3_REGION_NAME") or None,
                "access_key": os.environ.get("AWS_S3_ACCESS_KEY_ID")
                or os.environ.get("AWS_ACCESS_KEY_ID")
                or None,
                "secret_key": os.environ.get("AWS_S3_SECRET_ACCESS_KEY")
                or os.environ.get("AWS_SECRET_ACCESS_KEY")
                or None,
                "default_acl": "private",
                "querystring_auth": True,  # signed URLs so unvetted media stays private
                # Signed URLs expire (default 1 h; override with the seconds
                # below). Non-issue in practice: polling boards refetch the
                # snapshot every 1.5 s and WS boards get a fresh one on every
                # mutation, so URLs are always young. See DEPLOY.md.
                "querystring_expire": int(os.environ.get("AWS_QUERYSTRING_EXPIRE", "3600")),
            },
        },
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }

# --- Upload limits (bytes) enforced by validators in trivia/validators.py ---
# Handoff #7 §F2 policy (owner-approved): numbers live here, enforcement in
# trivia/validators.py + trivia/images.py so the direct create/PATCH path and
# the bulk zip path behave identically.
MAX_IMAGE_BYTES = 10 * 1024 * 1024     # 10 MB hard reject (was 5; now the reject ceiling)
MAX_AUDIO_BYTES = 8 * 1024 * 1024      # 8 MB hard reject (~8 min of 128 kbps MP3)
MAX_VIDEO_BYTES = 25 * 1024 * 1024     # 25 MB hard reject (20-30 s of 720p H.264 = 5-15 MB)
MAX_IMAGE_PIXELS = 40_000_000          # ~40 MP decode ceiling — decompression-bomb guard
IMAGE_RESIZE_THRESHOLD_BYTES = 1 * 1024 * 1024  # resize when bigger than this...
IMAGE_MAX_DIMENSION = 1920             # ...or when the longest edge exceeds this
IMAGE_JPEG_QUALITY = 82                # re-encode quality for photographic input

# Django's DATA_UPLOAD_MAX_MEMORY_SIZE covers the NON-file part of a request
# body (per the Django docs, multipart file data is excluded from this check
# and spills to disk via FILE_UPLOAD_MAX_MEMORY_SIZE instead). Actual upload
# ceilings are enforced where the sizes are knowable: per-file caps above via
# trivia/validators.py, the bulk zip/CSV caps in trivia/bulk_upload.py
# (MAX_ARCHIVE_BYTES = 200 MB compressed), and the reverse proxy's request
# body limit in production. 10 MB of form fields/JSON is far more than any
# endpoint here legitimately sends.
DATA_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024

# Honor the reverse proxy's X-Forwarded-Proto so request.is_secure() (and the
# admin's redirects/CSRF checks) see https when Caddy terminates TLS (§H).
# Opt-in via env: enabling it while NOT behind a trusted proxy would let
# clients spoof the header, so the dev default stays off.
# §I2 (Handoff #10): now ALSO a named settings constant — the auth throttles
# read it at call time (so tests can override_settings it) to decide whether
# X-Forwarded-For identifies the client. Getting this wrong throttles the
# whole site as one IP, so it shares the exact env flag that already gates
# SECURE_PROXY_SSL_HEADER: behind Caddy, both are true together.
DJANGO_BEHIND_PROXY = env_bool("DJANGO_BEHIND_PROXY", False)
if DJANGO_BEHIND_PROXY:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    # §I4: `check --deploy` wins that are safe exactly when TLS terminates in
    # front of us — session/CSRF cookies only ever travel over https then.
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True

# §I4: HSTS is a ONE-WAY DOOR (browsers pin https for the whole max-age), so
# it stays the OWNER's flag to flip: env-driven, default 0 (off). Suggested
# prod value once confident: 31536000 (see DEPLOY.md).
SECURE_HSTS_SECONDS = int(os.environ.get("SECURE_HSTS_SECONDS", "0"))
if SECURE_HSTS_SECONDS:
    SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool("SECURE_HSTS_INCLUDE_SUBDOMAINS", False)

# --- Paid tiers (see accounts/quotas.py) -----------------------------------
# Per-plan quotas. None = unlimited. Editing this dict changes pricing without
# a migration; enforcement handles None from day one.
#
# Product decision: game hosting stays unlimited for everyone for now (it was
# never gated), so free's games_per_month is None. To turn on free-tier game
# limits later, just set it to a number (e.g. 5) — the enforcement, profile
# usage block, and frontend meters all already handle it.
# §F3: "storage_bytes" caps the summed size of a user's stored media (counted
# from the persisted Question.media_bytes / Category.photo_bytes columns —
# never by listing the bucket). Free is 0 (free can't upload media anyway —
# its categories/questions quotas are 0); None = unlimited stays supported.
# A missing key also reads as unlimited so older PLAN_LIMITS overrides in
# tests keep working.
PLAN_LIMITS = {
    "free":    {"games_per_month": None, "categories": 0,  "questions": 0,   "storage_bytes": 0},
    "creator": {"games_per_month": None, "categories": 25, "questions": 500, "storage_bytes": 500 * 1024 * 1024},
}

# §G: hard cap on player (team) seats per game, enforced atomically in the
# join view. A settings constant, not a per-game field (configurability is
# punted). The host's control seat does NOT count. Exposed to clients as the
# snapshot's top-level `max_players` so frontends never hardcode the number.
MAX_PLAYERS_PER_GAME = 6

# --- Transactional email (Handoff #9 §K): Resend via Anymail ---------------
# Env-driven with a console fallback, so dev and the test suite never need a
# key (Django's test runner swaps in locmem regardless). Set RESEND_API_KEY
# to flip to real sending. Resend API keys are ACCOUNT-scoped; domains are
# verified per-domain within the account — the existing eventquiry key can
# send for drinkquiry.com once that domain (+ its SPF/DKIM DNS records) is
# added in the Resend dashboard. Until then, DEFAULT_FROM_EMAIL can point at
# the already-verified eventquiry.com as a stopgap (works, but off-brand and
# the from-domain/link-domain mismatch reads phishy — see CHANGES.md).
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
if RESEND_API_KEY:
    EMAIL_BACKEND = "anymail.backends.resend.EmailBackend"
    ANYMAIL = {"RESEND_API_KEY": RESEND_API_KEY}
else:
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "Drinkquiry <hello@drinkquiry.com>")
# Where password-reset links point (the SPA's public origin). The dev default
# is the Vite dev server.
FRONTEND_BASE_URL = os.environ.get("FRONTEND_BASE_URL", "http://localhost:5173").rstrip("/")

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Logging (§I5, Handoff #10) ----------------------------------------------
# A modest console setup: everything reaches stdout so `docker compose logs
# api` sees it — including accounts.emails' send-failure warnings (verified
# with a forced failure locally). Django's own request-error logging keeps
# working: the `django` logger propagates to root by default and
# disable_existing_loggers stays False. INFO root in prod, DEBUG-friendly in
# dev. Sentry/aggregation is §M.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "console": {"format": "{levelname} {asctime} {name} {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "console"},
    },
    # INFO root in dev too: a DEBUG root drowns the console in third-party
    # chatter (asyncio's selector line on every command, SQL echo). Raise a
    # specific logger when actually debugging.
    "root": {"handlers": ["console"], "level": "INFO"},
}
