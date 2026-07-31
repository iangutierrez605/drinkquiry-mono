# Drinkquiry

Quiz-show-style trivia platform where correct answers let you assign drinks (or points) to other teams. Django 6 + DRF + Channels backend, fully dockerized.

Support: **support@drinkquiry.com** (the address in the site footer — the mailbox itself is a forward/alias at the domain's mail hosting; Resend only sends).

## Quick start

```bash
cp .env.example .env          # edit DJANGO_SECRET_KEY at minimum
docker compose up --build
docker compose exec api python manage.py seed_demo        # 5 official categories x 5 questions
docker compose exec api python manage.py createsuperuser  # for /admin moderation
```

API at http://localhost:8000/api/, admin at http://localhost:8000/admin/, WebSockets at ws://localhost:8000/ws/game/CODE/. `GET /api/health/` is the deploy probe.

## Architecture

```
docker-compose
├── db      Postgres 17
├── redis   Redis 7 (Channels group layer — fans buzzes out to every screen)
└── api     Django 6 (ASGI). Dev: runserver. Prod: daphne (HTTP + WebSockets)
```

Apps:

- `accounts` — email-login user, knox tokens, `plan`/`plan_expires_at` paid tier (quotas in `settings.PLAN_LIMITS` + per-user `limit_overrides`), password forgot/reset/change with transactional email, per-IP throttles on the public auth endpoints, the staff user-management API, and venue branding (`brand_name`/`brand_logo`, creator plan)
- `trivia` — Category and Question (a question can live in SEVERAL categories) with owner, visibility, a full staff moderation queue + searchable library, soft delete on both models, versioned revise, host flags, bulk CSV/zip upload with media, staff-curated themes for one-tap board building
- `games` — Game, board columns/cells, participants (no account needed to buzz), buzz log, drink assignments, host kick (soft removal), game history + host-private reports, WebSocket consumer + REST polling snapshot

## The buzzer flow

1. Host creates a game (REST) → gets a 6-char join code + host participant token.
2. Players open `game/buzzer/<CODE>` in your frontend → `POST /api/games/<CODE>/join/ {name}` → participant token, stored in localStorage.
3. Everyone connects to `ws://…/ws/game/<CODE>/?token=<participant_token>`.
4. Host picks a tile (`open_cell`) — question shows on the board, buzzer LOCKED while the host reads.
5. Host sends `open_buzzer` — first buzz wins; every buzz is written to the DB with server receive time, so the full ordered list is broadcast and survives reloads.
6. Host judges (`judge`), reveals the answer (`reveal_answer`), assigns drinks (`assign_drinks`, drinks mode) and closes the tile (`close_cell`).

**Reload safety:** every mutation persists first, then the server broadcasts a full state snapshot. Clients are pure renderers of `state`, so a refreshed page (REST `GET /api/games/<CODE>/` or WS reconnect) is instantly consistent — scores, drink tallies, answered tiles, current buzz order, everything.

## REST API

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | /api/auth/register/ | – | Create account |
| POST | /api/auth/login/ | – | knox token |
| POST | /api/auth/logout/ | token | Invalidate token |
| GET/PATCH | /api/auth/profile/ | token | Own profile |
| GET/POST | /api/categories/ | token (POST quota-gated by plan) | List official+public+own; create own |
| GET/POST | /api/questions/?category=ID | token (POST quota-gated by plan) | Same visibility rules; supports multipart media upload |
| POST | /api/games/ | token | `{mode, categories:[ids], questions_per_category}` — 400 with per-category detail if any category is short on questions |
| GET | /api/games/CODE/ | – | Full board snapshot |
| POST | /api/games/CODE/join/ | – | `{name, participant_token?}` — join or reclaim seat |

## WebSocket actions

Player: `buzz`.
Host: `start_game`, `open_cell {cell_id}`, `open_buzzer`, `lock_buzzer`, `reset_buzzer`, `reveal_answer`, `judge {participant_id, correct}`, `assign_drinks {to_participant_id}`, `close_cell`, `finish_game`.
Server events: `state` (full snapshot), `answer_reveal {answer}`, `error {detail}`.

## Moderation (requirements 2, 3, 7)

Content is born PRIVATE. Setting visibility to public flips it to PENDING; it only appears publicly once an admin approves it in /admin (bulk approve/reject actions). Any edit to public content re-enters the queue. Private content is always usable by its owner regardless of moderation. Media size caps: 10 MB images (auto-resized past 1 MB / 1920 px), 8 MB audio, 25 MB video (tune in settings). With `MEDIA_BACKEND=s3`, files go to S3/DigitalOcean Spaces as private objects served through signed URLs, so unvetted uploads are never publicly listable.

## Monetisation: paid tiers (requirement 1)

`User.plan` ("free" / "creator") plus optional `plan_expires_at` is the source of truth
for entitlements; per-plan quotas live in `settings.PLAN_LIMITS` (`None` = unlimited), so
pricing changes are a settings edit, not a migration. **Manual grant flow: set `plan` on
the user in /admin** (this replaces the old "tick is_creator" workflow — that column is
gone; `user.is_creator` is now a derived property meaning "effective plan isn't free").
An expiry date in the past makes the account behave as free everywhere.

Enforcement is server-side: game creation checks `games_per_month`; category/question
creation checks the content quotas and returns a structured
`403 {"detail", "code": "quota_*", "used", "limit"}` that the frontend turns into
friendly limit/upsell copy. `GET /api/auth/profile/` reports `plan` (the *effective*
plan), `plan_expires_at`, and a `usage` block the UI renders as meters. Today game
hosting is unlimited for everyone (free `games_per_month: None`); flip it to a number in
`PLAN_LIMITS` to enable free-tier game limits — everything downstream already handles it.
When you add Stripe, a checkout webhook writes the same two fields and nothing else
changes. Quota tests live in the full suite: `python manage.py test accounts trivia games`.

The React + Vite frontend (in `frontend/`) covers the whole flow: /host (create with themes, lobby preview + swap, live panel, player kick), /board/CODE (TV — WebSocket with REST-polling fallback, venue branding), /game/buzzer/CODE (phones, no account), /profile (history, reports, branding, password), /moderate (six staff tabs).

## Roadmap / not in this prototype

- Stripe checkout + webhook to set `plan`/`plan_expires_at` (expiry is already enforced at read time) — includes billing the branding/venue tier
- Email verification at registration (password reset/change already ship)
- CAPTCHA on registration (per-IP auth throttles already ship; game join and the polling snapshot stay deliberately unthrottled)
- Team rosters (currently one participant = one team buzzer, which matches your design)
- Production hardening beyond DEPLOY.md's compose + Caddy setup

## Local dev without Docker

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate && python manage.py seed_demo
python manage.py runserver   # SQLite + in-memory channel layer fallback
```
