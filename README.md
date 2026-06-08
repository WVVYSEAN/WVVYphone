# WVVYphone

AI-powered outbound CRM for phone-first lead outreach and personalised cold email campaigns. Built on Django, deployed on Railway at [wvvy.pro](https://wvvy.pro).

## What it does

- **Lead generation** — search LinkedIn via Apify and import contacts directly into the CRM (phone number required at import; emails validated via ZeroBounce)
- **Phone-first workflow** — each lead has a dedicated detail page with a one-tap `tel:` call link, called/outcome tracking, and a contact log
- **Cold lead list** — server-side search, multi-field filter builder, quick-filter chips, multi-level sort panel, saved filter pills with emoji, relative time display, and **AI-powered natural language search** (Claude Haiku ranks leads by relevance with on-page score badges)
- **Automated outreach** — send personalised email sequences to imported leads via Resend
- **AI email replies** — inbound replies are handled by an AI pipeline that drafts or sends responses
- **Multi-workspace** — each team gets an isolated workspace with its own contacts, settings, and API keys
- **Invite-only access** — Google OAuth login, restricted to invited email addresses

## Stack

| Layer | Technology |
|---|---|
| Backend | Django 5, Python |
| Database | PostgreSQL (Railway) / SQLite (local) |
| Email | Resend API |
| Lead import | Apify (LinkedIn scraper, actor `T1XDXWc1L92AfIJtd`) |
| Email validation | ZeroBounce (batch API during import) |
| Background tasks | Python threads (within gunicorn) |
| Static files | WhiteNoise |
| Deployment | Railway |

## Local setup

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
source .venv/Scripts/activate   # Windows
# source .venv/bin/activate     # Mac/Linux

# 2. Install dependencies
pip install -r requirements.txt

# 3. Create a .env file and fill in values (see Environment variables below)
cp .env.example .env

# 4. Run migrations and start the dev server
python manage.py migrate
python manage.py runserver
```

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `SECRET_KEY` | Yes | Django secret key — generate with `python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"` |
| `DEBUG` | No | Set to `true` for local dev (default: `false`) |
| `SITE_URL` | Yes | Full origin of the deployed app, e.g. `https://yourapp.up.railway.app` — used for Google OAuth redirect URI |
| `MASTER_EMAIL` | Yes | Email address with implicit admin rights across all workspaces |
| `GOOGLE_LOGIN_CLIENT_ID` | Yes | Google OAuth client ID |
| `GOOGLE_LOGIN_CLIENT_SECRET` | Yes | Google OAuth client secret |
| `DATABASE_URL` | Yes (prod) | PostgreSQL connection URL — injected automatically when a Railway Postgres service is linked |
| `APIFY_API_TOKEN` | Yes | Apify API token for LinkedIn scraping |
| `APIFY_WEBHOOK_SECRET` | Yes | Secret for validating Apify webhook payloads |
| `ZEROBOUNCE_API_KEY` | Yes | ZeroBounce API key for email validation during import |
| `ANTHROPIC_API_KEY` | No | Anthropic API key — required for AI-powered natural language search on the cold lead list |
| `REDIS_URL` | No | Redis connection URL (only needed if Celery worker is running) |

Per-workspace settings (Resend API key, outreach templates, AI config) are stored in the database via the Settings page.

## Deployment

Deployed as a single Railway service. The `web` process in the Procfile runs migrations, collects static files, and starts gunicorn:

```
web: python manage.py migrate --noinput && python manage.py collectstatic --noinput && gunicorn config.wsgi
```

**Important:** add a PostgreSQL service to the Railway project so that `DATABASE_URL` is injected. Without it the app falls back to SQLite on the ephemeral container filesystem and data is lost on every redeploy.

## Key concepts

**Workspaces** — all CRM data is scoped to a workspace. Users belong to workspaces with roles (`owner`, `admin`, `member`). The active workspace is tracked in the session.

**Lead import flow** — user fills in the Advanced Search form → Apify runs the LinkedIn scrape → webhook fires on completion → contacts are imported (phone required), emails are validated through ZeroBounce (invalid emails are stripped; only `valid` emails get outreach), all tracked by a `TaskJob` record with a live progress bar in the UI.

**Phone-first lead list** — `/contacts/cold_lead/` is a dedicated list page with:
- Global search across name, email, and company
- Always-visible pills row: system quick-filter chips (dashed border) + user saved-filter pills with emoji (solid border)
- Multi-row filter builder with contextual operators per field type, including expanded date operators (Is Between, Is This Week, Is This Month, In the Next X Days, etc.)
- Multi-level sort panel (up to 5 levels) with field + direction dropdowns; column-header clicks for quick single-level sort
- Relative time display for Added / Last Edited date columns ("Today", "2 days ago") with full timestamp on hover
- Saved filter sets per user (up to 25), persisted to the `SavedFilter` model; "Update [name]" / "Save as New…" workflow when a pill is active
- **AI Search** — natural language input powered by Claude Haiku; extracts industry, location, role, company, and date criteria; scores and re-ranks all workspace leads without a page reload; matched rows show a purple relevance % badge

**Contact detail page** — `/contact/<pk>/` shows a full lead profile with a sticky call bar, `tel:` link, called/outcome toggles, contact log, and financials tab.

**Settings** — each workspace configures its own Resend API key, email templates, scoring thresholds, and AI behaviour via the Settings page (`/settings/`).
