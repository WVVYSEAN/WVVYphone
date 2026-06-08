# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Activate venv (always required first)
source .venv/Scripts/activate

# Dev server
python manage.py runserver

# Migrations
python manage.py migrate
python manage.py makemigrations

# Tests
python manage.py test crm
python manage.py test crm.tests.ClassName.test_method_name   # single test

# Static files (production)
python manage.py collectstatic --noinput

```

## Architecture

**Single Django app** (`crm/`) with all models, views, URLs, and templates in one place. The backend is `config/` (settings, urls, wsgi). Deployed on Railway with PostgreSQL + Redis; SQLite used locally.

### Auth & Access Control

All login is **Google OAuth only** — no username/password. The flow lives in `google_login` / `google_callback` in `crm/views.py`. Access is invite-only: `google_callback` checks `InvitedEmail` before creating a session.

`MASTER_EMAIL` (set in `config/settings.py`) bypasses the `InvitedEmail` check and has implicit admin rights everywhere.

Two decorators gate views:
- `@workspace_required` — for page views; redirects on failure; injects `workspace, membership` kwargs
- `@_api_workspace_required` — for JSON endpoints; returns 401/403 on failure; same kwargs

```python
@_api_workspace_required
@require_POST
def my_endpoint(request, workspace, membership):
    ...
```

Admin-level checks use `_is_admin(request, membership)` which returns True for MASTER_EMAIL or owner/admin roles.

### Multi-Tenancy

Every piece of CRM data (`Contact`, `Company`, `Opportunity`, `EmailThread`, etc.) has a `workspace` FK. All queries must be scoped: `Contact.objects.filter(workspace=workspace, ...)`.

`WorkspaceMembership` links users to workspaces with roles: `owner`, `admin`, `member`. The active workspace is tracked in `request.session['active_workspace_id']`.

### Settings / Configuration

Per-workspace configuration lives in `HeatSettings` (one-to-one with `Workspace`). Use `HeatSettings.get_for_workspace(workspace)` — **never** `HeatSettings.get()` (that's a legacy global singleton at pk=1 and will use the wrong workspace's thresholds/keys). This applies to `auto_heat()` too — always pass the workspace cfg as the second argument: `auto_heat(contact, cfg)`.

HeatSettings holds: Resend API key, scoring thresholds, outreach templates, AI config, calendar booking URL.

### Resend Email Integration

`resend.api_key = cfg.resend_api_key` then `resend.Emails.send({...})` — set inline before each call, not globally. The `from` address must be a Resend-verified domain. Workspace invite emails use `workspace.heat_settings`.

Note: setting `resend.api_key` is a module-level mutation. This is safe with gunicorn sync workers (one request per worker) but would be a race condition under async workers (gevent/eventlet). Do not switch to async workers without addressing this.

### Background Tasks

Long-running operations (Apify lead import + ZeroBounce email clean + outreach emails) run as **background threads** within the gunicorn process — not Celery. Task functions live in `crm/tasks.py` and are called directly via `threading.Thread(target=fn, args=(...), daemon=True).start()` from `apify_webhook` and `backup_outreach` in `views.py`.

**`TaskJob` model** tracks progress with two phases:
- `phase='importing'` — fetching leads from Apify dataset, validating emails via ZeroBounce, writing Contact records
- `phase='emailing'` — sending outreach emails to imported contacts with valid emails

Progress is written to the DB every 50 records (`_PROGRESS_INTERVAL`). The frontend polls `/api/tasks/<job_pk>/status/` every 2s.

Key rules:
- Use `get_or_create` when creating a TaskJob linked to an ApifyRun (`apify_webhook` can fire twice on Apify retries)
- Import `_maybe_send_outreach` inside the task function body (not at module top) to avoid circular imports between `tasks.py` and `views.py`

Note: `crm/tasks.py` still uses Celery `@shared_task` decorators but `.delay()` is never called — tasks are invoked directly as plain functions via threads. Do not reintroduce `.delay()` without a dedicated Railway worker service.

### AI / Inbound Email Pipeline

Inbound emails hit `inbound_webhook` → `_handle_inbound` → `_send_ai_reply` → `_do_send_ai_email`. The `AICallLog` model queues drafts when `ai_review_mode=True`. Capture `was_already_stopped = contact.sequence_stopped` **before** mutating the contact to correctly gate AI replies.

Contact matching in `_handle_inbound`: prefer `reply+{pk}@` address tag (unambiguous, pk is globally unique), fall back to sender email (ordered by most-recent outbound thread to resolve cross-workspace duplicates).

### Apify Integration

The actor ID is `T1XDXWc1L92AfIJtd`. Strict enum fields: `seniority`, `functional` — use exact values from `_SENIORITY_OPTIONS` / `_FUNCTIONAL_OPTIONS` in `views.py`. Industry is free-text via `industryKeywords`. Do not add `personState` or `companyState` filters — Apify's enum values for these are unusable LinkedIn-internal region strings.

Webhook flow: Apify fires `ACTOR.RUN.SUCCEEDED` → `apify_webhook` sets dataset ID and spawns a background thread running `run_apify_import` → task imports contacts and sends outreach → `TaskJob` tracks progress.

### ZeroBounce Integration

During `run_apify_import`, emails from each Apify dataset page are batch-validated via ZeroBounce (`crm/services/zerobounce.py`). Service layer rules:
- **Keep email on contact** if status is `valid` or `catch-all`
- **Strip email** (but still import contact if phone present) for `invalid`, `spamtrap`, `abuse`, `do_not_mail`, etc.
- **Automated outreach** only for `valid` emails
- Result stored in `Contact.email_status` as `status` or `status:sub_status`
- Requires `ZEROBOUNCE_API_KEY`; if unset, emails are kept with status `unverified` and outreach is skipped

### Phone-First Workflow

The app is phone-call-first. Leads are only imported from Apify if they have a phone number (`tasks.py` filters at ingestion — no phone = skipped). The lead list navigates to `/contact/<pk>/` (a full dedicated page) instead of opening a modal.

**Contact detail page** (`crm/templates/crm/contact_detail.html`, view `contact_detail` in `views.py`):
- Sticky top bar: heat badge + Mark Called toggle
- Hero: phone as primary CTA (`tel:` link), email, drip toggle
- Three tabs: Overview, Contact Log, Financials & PE Data
- Design uses DM Sans/DM Mono fonts scoped under `.ldp` class — does not inherit app-wide Syne font

**New Contact fields** (migration `0032_phone_first_workflow`):
- `called` (bool) — toggled via `POST /api/contact/<pk>/called/`
- `call_outcome` (choices: interested/not_now/not_interested/booked/no_answer) — `POST /api/contact/<pk>/outcome/`
- `email_outreach_enabled` (bool, default False) — `POST /api/contact/<pk>/email-outreach/`
- `revenue`, `ebitda`, `company_size`, `ownership_structure`, `reason_for_sale`, `causality_notes` — saved via `POST /api/contact/<pk>/financials/`

**New TouchPoint fields**: `outcome` (same choices as call_outcome). `voicemail` and `text` added to `TOUCHPOINT_CHOICES`. The contact log tab uses the existing `POST /api/touchpoint/contact/<pk>/` endpoint with the new `outcome` field.

**Email outreach default**: `email_outreach_enabled` defaults to `False` for all contacts. This is separate from `ai_managed` (which controls AI auto-replies to inbound). Do not conflate them.

### Cold Lead List — Search, Filter & Sort

The cold lead list lives at `/contacts/cold_lead/` and is served by `cold_lead_list` in `views.py` (registered **before** the wildcard). All filtering and sorting is **server-side** — no client-side table engine.

**Features:**
- Global search (name, email, company) — debounced 300 ms, auto-submits the form
- **Pills row** — always-visible row with two zones: system chips (dashed border) and user saved-filter pills (solid border with emoji)
  - System chips: Ready to Call (amber hero), Hot Leads, Responded, Added This Week, Not Yet Contacted
  - User saved-filter pills apply a full saved state and set `active_pill_id` in the URL; delete × on each pill
- Filter builder — multi-row, field + operator + value; operators contextual by field type; AND/OR logic toggle at 2+ rows
- **Sort panel** — slide-down multi-level sort (up to 5 levels) with field + direction dropdowns; toolbar button shows active level count badge; column-header clicks reset to single-level
- Saved filter sets — persisted per user via `SavedFilter` model (max 25); save with emoji via modal; "Update [name]" / "Save as New…" toolbar buttons when a pill is active
- **Relative time** for date columns — "Today", "Yesterday", "3 days ago" etc. with full timestamp on hover

**`SavedFilter` model** (migrations `0033_savedfilter`, `0034_contact_updated_at_savedfilter_emoji`):
- Fields: `workspace`, `user`, `name`, `emoji` (max 8 chars), `filter_state` (JSONField), `created_at`
- `filter_state` stores: `{q, chips, filters:[{field,op,val}], filter_logic, sort_levels:[{field,dir}]}`
- Unique on `(workspace, user, name)`

**`Contact.updated_at`** (migration `0034`) — `auto_now=True` DateTimeField; used by "Date Last Edited" column and filter field.

**API endpoints:**
- `POST /api/saved-filters/save/` — upsert by name; body `{name, emoji, state}`. Pass `update_id` (int pk) instead of `name` to update an existing filter in-place (used by "Update" button)
- `POST /api/saved-filters/<pk>/delete/` — delete owned filter
- `POST /api/ai-contact-search/` — AI natural language search; body `{query: string}`; returns `{explanation, matches:[{pk,score}], total}` scored across all cold_lead contacts in the workspace

**AI Search** (`POST /api/ai-contact-search/`):
- Sends the natural language query to Claude Haiku (`claude-haiku-4-5-20251001`) which extracts structured criteria: `industry_terms`, `location_terms`, `company_terms`, `role_terms`, `size_terms`, `org_type_terms`, `min_founded_year`, `max_founded_year`, and `explanation`
- Scores all workspace cold-lead contacts in Python (no extra DB queries per contact); industry hits weight ×3, location ×2, company ×2, role/size/org_type ×1; founded year in-range gives +2 bonus, out-of-range −3 penalty
- Requires `ANTHROPIC_API_KEY` environment variable; returns 400 if not set
- Client-side: table rows are reordered by score (non-matches hidden), matched rows get a purple % relevance badge in the Name column, a banner shows the AI explanation + match count. `clearAiSearch()` restores original DOM order. `_origRowOrder` array stores the original `<tr>` sequence on first AI run; `_aiMode` boolean guards subsequent clears.

**Filter helper `_build_filter_q(field, op, val)`** in `views.py` returns a `Q` object for one row. Supported fields: `name`, `email`, `company`, `role`, `location`, `industry`, `heat`, `called`, `call_outcome`, `phone`, `created_at`, `updated_at`.

Supported date operators: `is_date`, `is_not_date`, `is_before`, `is_on_or_before`, `is_after`, `is_on_or_after`, `is_between` (val = `"YYYY-MM-DD,YYYY-MM-DD"`), `is_this_week`, `is_this_month`, `in_last_x_days`, `in_next_x_days`, `has_no_value`. All date ops use the `db` variable so they work for both `created_at` and `updated_at`.

**URL params format:** `q=`, `chip=` (repeatable), `ff0=`/`fo0=`/`fv0=` per filter row, `filter_logic=AND|OR`, `s1=`/`d1=` … `s5=`/`d5=` for sort levels, `active_pill_id=<int>`. Old `sort=`/`sort_dir=` params still work as a single-level fallback. The server reads `ff*` keys via regex so row deletions leaving index gaps are handled correctly.

**Pagination:** 100 per page via Django's `Paginator`.

**JS init rule — do not call `submitForm()` during page load.** `addFilterRow` accepts an `autoSubmit` parameter (default `true`). Always pass `false` when restoring rows from server state on `DOMContentLoaded`, otherwise every render with active filters triggers an immediate re-submit → infinite reload loop. The table headers are always rendered (empty state uses `{% empty %}` inside `<tbody>`) so sort columns remain clickable even when filters return 0 results.

**Sort state in JS** — `_sortLevels` array (`[{field, dir}]`) is the single source of truth. `syncSortHiddenInputs()` writes `s1/d1`… hidden inputs before each form submit. `_activePillId` (int or null) tracks which saved filter pill is loaded; when set, the toolbar shows "Update [name]" + "Save as New…" instead of "Save Search".

### URL Ordering

The wildcard route `<str:model_type>/<str:stage>/` must stay **last** in `crm/urls.py` — it catches paths like `/contact/cold_lead/`. All `/api/...` routes must be declared above it. The contact detail route `contact/<int:pk>/` and the cold lead list route `contacts/cold_lead/` must also be above the wildcard.

### Migration Conventions

For columns that may already exist in production Postgres, use `SeparateDatabaseAndState` + `RunPython` with a vendor check:

```python
def _add_column(apps, schema_editor):
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute("ALTER TABLE crm_x ADD COLUMN IF NOT EXISTS ...")
    else:
        # SQLite: PRAGMA table_info check
        with schema_editor.connection.cursor() as cursor:
            cols = {row[1] for row in cursor.execute("PRAGMA table_info(crm_x)")}
            if 'col' not in cols:
                cursor.execute("ALTER TABLE crm_x ADD COLUMN ...")
```

This keeps migrations idempotent across both local SQLite and production Postgres.

### Frontend

Server-rendered Django templates with Tailwind CSS (CDN) and vanilla JS — no frontend build step. Custom styles in `crm/static/crm/wvvy.css`. Settings page sections are shown/hidden via `showSection(id)` JS; the active section id is stored in `?section=` query param. The `buildPayload()` function in settings.html collects `[data-str-field]`, `[data-bool-field]`, `[data-field]` inputs — password and URL type inputs are skipped if blank to avoid overwriting saved values.

Advanced Search page polls `/api/tasks/<job_pk>/status/` every 2s while a TaskJob is active, rendering a two-phase progress bar (0–50% import, 50–100% email) in the status cell of each search row.

**base.html globals** — the following are available on every page and must not be removed:
- `getCsrf()` — reads `csrftoken` cookie; used by all fetch calls
- `showToast(msg, isError)` — bottom-right toast via `#save-indicator`
- `formatDate(iso)` — formats `YYYY-MM-DD` to `Mon D, YYYY`
- `escHtml(str)` — HTML-escapes a string
- `HEAT_OPTIONS` / `STAGE_OPTIONS` — metadata arrays mirroring Python constants

The **history modal has been removed** from `base.html`. There is no longer an `openHistory()` function or `#history-modal` element. Contact rows navigate to `/contact/<pk>/` (the dedicated detail page). Company rows in `stage_list.html` show the name as plain text (no modal trigger).
