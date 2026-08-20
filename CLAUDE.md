# CLAUDE.md — WeAreChecking

Reference document for AI-assisted development on this project.
Read this file before making any changes.

---

## Project overview

**WeAreChecking** (`wearechecking.gg`) is a web application for
sim racing teams to plan and manage driver stints for endurance
races. It replaces spreadsheet-based coordination with a purpose-
built tool for event creation, driver signup, availability
collection, and stint assignment.

This is a community tool for CRACKD Racing with no commercial
ambitions. The target audience is sim racers — tech-savvy,
perpetually online, Discord-native.

**Current version:** v0.1.0

---

## Tech stack

| Layer | Choice | Notes |
|---|---|---|
| Language | Python 3.13 | |
| Framework | Django 6.0.x | `AUTH_USER_MODEL = 'events.User'` |
| Database | MySQL (Railway) / MariaDB (local) | mysqlclient, PyMySQL fallback |
| Interactivity | HTMX 2.x | Server-driven partial updates |
| Reactivity | Alpine.js 3.x | Client-side state, no build step |
| CSS | Tailwind CSS v4 | CLI binary, no Node required |
| Auth | django-allauth 65.x | Discord OAuth only |
| Static files | Whitenoise | Served from gunicorn directly |
| Deployment | Railway (Railpack) | MySQL add-on, auto-deploy on push |
| Domain | wearechecking.gg | Namecheap, CNAME to Railway |
| Errors | Sentry (sentry-sdk 2.x) | Inert unless `SENTRY_DSN` is set |
| Logs | stdout JSON | Captured by Railway; see Observability |

**Dependencies are exact-pinned in `backend/requirements.txt`.** Railway
rebuilds on every push, so a floating version lets a deploy change behaviour
with no code change. Bump a pin deliberately, test, then push. Note that
`django-allauth` 65.x is not "0.65" — the project moved to calendar-style
major versions, and the old `>=0.61.0` floor silently allowed 64 majors of
drift.

---

## Project structure

```
endurance-planner/
├── backend/                    # Django project root
│   ├── config/
│   │   ├── settings.py         # All configuration
│   │   ├── logging.py          # JsonFormatter, RequestIdFilter,
│   │   │                       #   Sentry admin-key scrubber
│   │   ├── middleware.py       # CanonicalHostMiddleware,
│   │   │                       #   RequestLogMiddleware
│   │   ├── urls.py             # Root URL config
│   │   ├── test_settings.py    # SQLite in-memory overrides for tests
│   │   └── wsgi.py
│   ├── events/                 # Main app — all models and views
│   │   ├── models.py           # User, Event, Driver, Availability,
│   │   │                       #   StintAssignment, Feedback, ActivityLog
│   │   ├── views.py            # All views
│   │   ├── urls.py             # All URL patterns
│   │   ├── activity.py         # Action-name map, log_detail()
│   │   ├── middleware.py       # ActivityLogMiddleware
│   │   ├── forms.py            # EventCreateForm
│   │   ├── utils.py            # Stint and availability-grid calculations
│   │   ├── adapters.py         # Discord OAuth adapter
│   │   ├── tests.py            # Full suite (928 tests, 129 classes)
│   │   ├── context_processors.py  # discord_user + login_next
│   │   ├── templatetags/
│   │   │   └── tz_filters.py   # to_utc_z, dict_get,
│   │   │                       #   seconds_to_hours_display, seconds_to_mmss
│   │   ├── migrations/         # 0001-0010
│   │   └── management/
│   │       └── commands/
│   │           ├── setup_discord_oauth.py
│   │           └── prune_activity.py
│   ├── templates/
│   │   ├── base.html           # Fixed header, footer, bg-grid, login modal,
│   │   │                       #   toast, message banners, feedback widget,
│   │   │                       #   theme toggle, htmx 422 swap opt-in
│   │   ├── home.html
│   │   ├── event_create.html
│   │   ├── signup.html
│   │   ├── signup_edit.html
│   │   ├── signup_success.html
│   │   ├── admin.html          # Combines event details + stint
│   │   │                       #   assignment (no separate page)
│   │   ├── view.html
│   │   ├── feedback_view.html
│   │   ├── activity_view.html
│   │   ├── admin_error.html
│   │   ├── 404.html
│   │   ├── 500.html
│   │   ├── socialaccount/      # allauth template overrides
│   │   │   ├── login.html
│   │   │   └── authentication_error.html
│   │   └── partials/           # HTMX swap targets and shared fragments
│   │       ├── signup_form.html
│   │       ├── signup_edit_form.html
│   │       ├── driver_list.html
│   │       ├── driver_name_display.html
│   │       ├── driver_name_edit_form.html
│   │       ├── admin_add_driver.html
│   │       ├── event_create_form.html
│   │       ├── event_create_success.html
│   │       ├── search_results.html
│   │       ├── form_errors.html          # shared 422 validation errors
│   │       ├── availability_warning.html # schedule move stranded availability
│   │       └── discord_icon.html
│   ├── static/
│   │   └── css/
│   │       ├── tailwind.css    # Source — @source directives,
│   │       │                   #   all tokens and component classes
│   │       └── output.css      # Compiled — committed to git
│   ├── railpack.json           # Railpack build config
│   ├── requirements.txt        # Exact-pinned
│   ├── .env                    # Local only — never committed
│   ├── .env.example
│   └── manage.py
├── design/                     # Design reference files — gitignored
│   ├── DESIGN_SYSTEM.md
│   ├── homepage.html
│   ├── admin.html
│   ├── view-event-v2.html
│   ├── create-event.html
│   ├── signup.html
│   ├── logo-refined.html
│   ├── palette-v2.html
│   └── typography.html
├── bin/                        # Tailwind CLI binaries — gitignored
│   ├── tailwindcss.exe         # Windows
│   └── tailwindcss             # Linux (for Docker if used)
├── docker-compose.yml          # Local DB option
├── Makefile                    # CSS build shortcuts
└── CLAUDE.md                   # This file
```

There is no `events/admin.py` — `django.contrib.admin` is not installed and
`/admin/` 404s by design (the path is used for this app's own admin pages).

---

## Data models

### User (extends AbstractUser)
```python
discord_id        CharField     # Discord snowflake, unique
discord_username  CharField     # Display name, updated on login
discord_avatar    CharField     # CDN URL
```
All Django auth fields inherited. Username is set to discord_id
for uniqueness. `AUTH_USER_MODEL = 'events.User'` in settings.

### Event
```python
id                    UUIDField     # Primary key, auto-generated
admin_key             CharField(20) # Random string, used in admin URL
name                  CharField
team_name             CharField     # Optional
game                  CharField     # Optional — iRacing, LMU, ACC etc.
date                  DateField
start_time_utc        TimeField     # Session start (warmup/qualifying)
race_start_time_utc   TimeField     # Optional — green flag, if later
length_seconds        PositiveIntegerField
car                   CharField     # Optional
track                 CharField     # Optional
setup                 TextField     # Optional
fuel_capacity         FloatField    # Optional — for stint calc
fuel_per_lap          FloatField    # Optional
tire_change_fuel_min  FloatField    # Optional
target_laps           PositiveIntegerField  # Optional
avg_lap_seconds       FloatField    # Optional
in_lap_seconds        FloatField    # Optional
out_lap_seconds       FloatField    # Optional
recruiting            BooleanField  # Show on home page
created_at            DateTimeField # auto_now_add
created_by            FK(User)      # Nullable — Discord user who created
```

**Session start vs race start.** `start_time_utc` is when the session opens;
`race_start_time_utc` is the green flag when it differs. Stint windows anchor
to the *race* start (`effective_start_datetime_utc`), but availability slots
always anchor to the *session* start so warmup and qualifying are selectable.
Changing the race start therefore re-evaluates existing availability rather
than invalidating it.

Key properties: `start_datetime_utc`, `end_datetime_utc`,
`effective_start_time_utc`, `effective_start_datetime_utc`,
`effective_end_datetime_utc`, `has_required_stint_fields`

### Driver
```python
id          UUIDField
event       FK(Event)
user        FK(User)      # Nullable — set if Discord-authenticated
name        CharField     # Editable even if Discord-linked
timezone    CharField     # IANA string e.g. America/New_York
signed_up_at DateTimeField
```

### Availability
```python
driver    FK(Driver)
slot_utc  DateTimeField   # UTC datetime of 30-min block start
```
Unique together: `(driver, slot_utc)`

### StintAssignment
```python
event            FK(Event)
stint_number     PositiveIntegerField  # 1-indexed
driver           FK(Driver)    # Nullable — unassigned stints allowed
condition        CharField     # 'dry' | 'mixed' | 'wet', default 'dry'
actual_start_utc DateTimeField # Nullable — manual start-time override.
                               #   When set, this and all later stints
                               #   cascade from it.
```
Unique together: `(event, stint_number)`

### Feedback
```python
text          TextField
page_url      CharField(500)
user_agent    CharField(500)
request_id    CharField(32)   # ID of the last request the reporter saw
submitted_at  DateTimeField
```
No IP address is stored. `request_id` arrives on the `X-Last-Request-Id`
header that `base.html` attaches to every HTMX request, so a report filed
right after a failure points at that failure's log line.

### ActivityLog
```python
occurred_at   DateTimeField   # auto_now_add, indexed
request_id    CharField(32)
action        CharField(64)   # 'signup.submit', 'admin.save_assignments'
method        CharField(8)
status_code   PositiveSmallIntegerField
duration_ms   PositiveIntegerField
user          FK(User)        # Nullable, SET_NULL
visitor_id    CharField(32)   # wac_vid cookie, anonymous stitching
event_id_ref  UUIDField       # Nullable - a bare UUID, deliberately not a FK
path          CharField(300)  # Admin keys redacted before storage
is_htmx       BooleanField
detail        JSONField       # Extras from log_detail()
```
One row per handled request. **`event_id_ref` is not a foreign key** and that
is deliberate: deleting an event is itself a recorded action, and a FK would
either cascade that history away or null out the identifier needed to read
it. `user` *is* a FK because Discord users are never deleted here and
`select_related()` keeps the dashboard to one query.

---

## URL structure

```
/                                            home
/create/                                     event creation
/search/                                     event search (HTMX)
/set-timezone/                               set admin_timezone cookie
/<event_id>/view/                            public view page
/<event_id>/signup/                          driver signup
/<event_id>/signup/<driver_id>/edit/         edit availability (URL key)
/<event_id>/signup/<driver_id>/success/      post-signup success
/<event_id>/signup/<driver_id>/delete/       remove driver
/<event_id>/my-availability/                 edit availability (Discord)
/<event_id>/admin/                           admin page (Discord or session)
/<event_id>/admin/<admin_key>/               admin entry point (key auth)
/<event_id>/admin/save-details/              save event detail fields
/<event_id>/admin/save-calc/                 save stint calc fields
/<event_id>/admin/save-assignments/          save stint assignments
/<event_id>/admin/add-driver/                add driver manually
/<event_id>/admin/remove-driver/<id>/        remove driver
/<event_id>/admin/edit-driver/<id>/          edit driver name
/<event_id>/admin/delete-event/              delete event (typed confirmation)
/<event_id>/admin/create-stints/             legacy — redirects to admin page
/<event_id>/stints/<n>/set-start/            live stint start override
/<event_id>/stints/<n>/reset-start/          clear stint start override
/feedback/submit/                            feedback form POST
/feedback/view/                              password-protected viewer
/client-error/                               browser error reports (POST)
/activity/view/                              password-protected usage dashboard
/healthz/                                    Railway healthcheck (no DB)
/accounts/                                   allauth URLs (Discord OAuth)
```

Admin sub-routes are declared **before** the `<str:admin_key>` entry point in
`events/urls.py`, or Django matches literal segments like `save-details` as
an admin key.

---

## Authentication model

Three parallel auth mechanisms coexist:

**1. Discord OAuth (recommended)**
- Login via `/accounts/discord/login/` → Discord → callback
- Sets Django session, populates `User` model
- Admin access: `event.created_by == request.user`
- Driver access: `driver.user == request.user`
- 30-day rolling session — `SESSION_COOKIE_AGE = 2592000` plus
  `SESSION_SAVE_EVERY_REQUEST = True`, so the window counts from last
  activity. Without the latter the session expires 30 days after *login*
  no matter how active the user is.

**2. Admin key URL (legacy / fallback)**
- Admin key embedded in URL: `/<event_id>/admin/<admin_key>/`
- Validated with `hmac.compare_digest()` for timing safety
- Sets session key `admin_{event_id} = True` on valid access, then 302s to
  the key-less `/admin/` URL so the key appears in logs only once
- Sub-routes use `require_admin_session()` helper

**3. Edit URL (drivers without Discord)**
- Driver edit URL contains driver UUID
- No additional auth — URL possession = access
- Works for manually-added drivers with no Discord account

### Session gotchas

**`cycle_key()` only on transition.** `_grant_admin_session()` rotates the
session ID when granting admin, but returns early if the flag is already set.
`cycle_key()` deletes the old session row, so calling it on every admin
request logs out any other tab or in-flight HTMX request — which presents to
the user as being randomly signed out.

**One hostname only.** Session and CSRF cookies are host-only
(`SESSION_COOKIE_DOMAIN` is deliberately unset), so a site reachable at both
the apex and `www` hands out cookies that work on just one of them. Set
`CANONICAL_HOST` and `config.middleware.CanonicalHostMiddleware` 301s
everything onto it. The middleware disables itself if `CANONICAL_HOST` is
unset, or is not in `ALLOWED_HOSTS` (which could only cause a redirect loop).

**Where the OAuth callback host comes from.** allauth builds it from the
*request*, not from the `django.contrib.sites` record — `build_absolute_uri`
only falls back to `Site.domain` when there is no request. `SITE_DOMAIN` sets
the Site row for absolute URLs built without a request; it does not steer
login. The SocialApp is resolved by `SITE_ID`, so a mismatched `Site.domain`
does not break OAuth.

**Discord `prompt=none`.** `SOCIALACCOUNT_PROVIDERS` sends `prompt=none` so
returning users skip the consent screen. When silent auth is impossible
(first authorization, revoked access, expired Discord session) Discord
returns an *error* rather than a consent screen. That lands on
`templates/socialaccount/authentication_error.html`, which offers a retry
that overrides the setting with `prompt=consent` via allauth's
`?auth_params=` query parameter.

**`requests` is an undeclared runtime dependency.** allauth imports it in its
socialaccount OAuth2 client but only declares it under an optional
`socialaccount` extra that is not installed. Discord login depends on the
explicit `requests` pin in `requirements.txt`. If a future allauth version
starts touching `allauth/socialaccount/internal/jwtkit.py` from the shared
OAuth2 path, switch to `django-allauth[socialaccount]` (it also pulls
`oauthlib` and `pyjwt[crypto]`).

---

## Key implementation patterns

### HTMX partial updates
HTMX handles form submissions, inline field editing, driver
removal, and search. The `django-htmx` middleware provides
`request.htmx` boolean. Views return full pages on direct
load and HTML fragments on HTMX requests.

**Validation errors return 422.** HTMX does not swap non-2xx responses by
default, so a view returning an error partial with 422 would render nothing
and the form would appear to do nothing at all. `base.html` registers an
`htmx:beforeSwap` handler that opts 422 in. All admin form endpoints use
`partials/form_errors.html` with status 422.

That handler deliberately does **not** clear `detail.isError`. HTMX derives
`afterRequest`'s `successful` flag from it, and forms key their "reset and
close" logic off that — clearing it would make a form wipe the user's input
and dismiss itself while displaying an error.

When a form's success target differs from where its errors belong (the
add-driver form swaps the driver list on success), the error response sets
`HX-Retarget` / `HX-Reswap` rather than adding bespoke client-side handlers.

**Event names must be kebab-case.** HTML lowercases attribute names, so an
Alpine `@someEvent.window` listener can never match a camelCase
`HX-Trigger`. Use `show-toast`, `feedback-success`.

### Feedback to the user
- **Toast** — one shared `.toast` in `base.html`, fired server-side with
  `HX-Trigger: {"show-toast": {"message": "...", "error": true}}` or
  client-side via `$dispatch('show-toast', {...})`. Transient.
- **Message banners** — `django.contrib.messages`, rendered in `base.html`.
  Used for things that must not vanish unread, like event deletion.
- **Inline partials** — `form_errors.html`, `availability_warning.html`.

### Alpine.js reactivity
Alpine handles:
- Dark/light theme toggling (`data-theme` attribute on `<html>`)
- Stint assignment table state (`stintPlanner()`, `driverDropdown()`)
- Stint calculation live preview (`stintCalc()`)
- Schedule-change availability warning (`scheduleWatch()`)
- Live stint table and time editing on the view page (`stintTable()`)
- Timezone picker in signup forms (`timezonePicker()`)
- Login modal (`$dispatch('open-login')`)
- Feedback widget
- Copy-to-clipboard buttons

The login modal lives in `base.html`, not `home.html` — the header login
button is on every page, so its listener has to be too.

### Timezone handling
All times stored in UTC. Client-side conversion via
`Intl.DateTimeFormat` API. ISO strings normalized with Z suffix
using `normalize_iso()` helper for consistent JS comparison.
Template filter `to_utc_z` formats datetimes for Alpine
consumption.

### The availability slot grid
`Availability` rows are absolute UTC datetimes on a 30-minute grid whose
origin is `slot_grid_anchor(event)` — the wall-clock `:00`/`:30` boundary at
or before the session start.

Flooring that anchor matters. If the grid took its phase from the exact start
time, editing an event from 12:00 to 12:15 would move every boundary to
`:15`/`:45` and orphan every stored slot — a fifteen-minute correction
destroying more availability than moving the event by an hour. Anything
comparing a stint time against stored slots must use the same floored anchor;
`_driver_has_conflict()` in `views.py` floors independently and the two must
agree.

Moving an event's **date** still strands availability, by design — drivers
need to re-enter it for a new day. `admin_save_details` detects that via
`_drivers_with_stale_availability()` and reports exactly who is affected
instead of failing silently.

### Tailwind CSS v4
Config uses `@source` directives in CSS, not `tailwind.config.js`.
Dark mode via `@variant dark` and `[data-theme="dark"]` selectors.
CSS custom properties (`--bg`, `--primary`, `--secondary` etc.)
drive all theming. `output.css` is committed to git since
Railway cannot run the Tailwind binary during build.

**Always rebuild after template changes:**
```powershell
# Windows dev
.\bin\tailwindcss.exe -i backend\static\css\tailwind.css `
  -o backend\static\css\output.css --minify

# Or via Makefile
make css
```

---

## Design system

**Typography:**
- Display/headings: Rajdhani Bold (700)
- Body/data/inputs: DM Mono (400/500)
- Loaded from Google Fonts in base.html

**Color tokens (CSS custom properties):**
```
--bg           Page background
--bg-raised    Header, elevated surfaces
--bg-card      Cards, table rows
--border       Subtle dividers
--border-mid   Focused borders
--text         Primary text
--text-mid     Secondary/metadata text
--text-dim     Placeholders, disabled
--primary      Orange (dark) / Pink (light) — CTAs, assigned stints
--secondary    Teal (dark) / Purple (light) — nav, available slots
--danger       Red — errors, unassigned stints
--assigned-bg / --assigned-text
--unassigned-bg / --unassigned-text
--avail-bg / --avail-text / --unavail-bg / --unavail-text
--partial-bg / --partial-text
```

**Component classes defined in tailwind.css:**
`btn-primary`, `btn-secondary`, `btn-ghost`, `card`,
`card-primary`, `card-secondary`, `form-card`, `field`,
`field-row`, `detail-field`, `stat-card`, `driver-row`,
`meta-pill`, `section-heading`, `avail-grid`, `avail-slot`,
`unified-table`, `user-pill`, `my-events-card`, `event-item`,
`toast`, and more.

**Sharp corners throughout** — `border-radius: 0` on all
cards, buttons, and inputs. No `rounded-*` Tailwind classes
on interactive elements.

**Design reference files** live in `design/` (gitignored).
Read these before making visual changes:
- `design/DESIGN_SYSTEM.md` — authoritative spec
- `design/homepage.html` — homepage reference implementation
- `design/admin.html` — admin page reference
- `design/view-event-v2.html` — view event reference
- `design/create-event.html` — create event reference
- `design/signup.html` — signup reference
- `design/logo-refined.html` — WAC logo variants

---

## Environment variables

### Required in all environments
```
DJANGO_SECRET_KEY           Strong random key (50+ chars)
DJANGO_DEBUG                True (dev) / False (prod)
ALLOWED_HOSTS               Comma-separated hostnames
DB_NAME                     Database name
DB_USER                     Database user
DB_PASSWORD                 Database password
DB_HOST                     Database host
DB_PORT                     Database port (default 3306)
```

### Required in production
```
CSRF_TRUSTED_ORIGINS        https://wearechecking.gg,https://www.wearechecking.gg,...
CANONICAL_HOST              wearechecking.gg — the single host users land on.
                              Without it, logging in on one host and browsing
                              on another silently drops the session.
                              ALLOWED_HOSTS must list this host AND every host
                              to be redirected — see the note below.
DISCORD_CLIENT_ID           From discord.com/developers
DISCORD_CLIENT_SECRET       From discord.com/developers
FEEDBACK_PASSWORD           Password for /feedback/view/
```

### Observability (all optional)
```
LOG_LEVEL                   Root log level. Default INFO.
LOG_FORMAT                  json | console. Defaults to console when
                              DJANGO_DEBUG is True, json otherwise. Keep json
                              in production: Railway's log browser only
                              searches text, so discrete keys are what make
                              status, route and request_id filterable.
SENTRY_DSN                  Unset means Sentry is entirely inert - nothing is
                              initialised and no network calls are made.
SENTRY_ENVIRONMENT          Label separating environments. Default production.
SENTRY_LOGS_LEVEL           Level forwarded to Sentry Logs. Default WARNING.
                              INFO ships the whole request stream; OFF ships
                              none. An unrecognised value falls back to
                              WARNING rather than to something louder — and
                              so does NOTSET, which is spelled correctly but
                              resolves to 0, meaning forward everything.
SENTRY_TRACES_SAMPLE_RATE   Performance tracing, 0 to 1. Default 0; this is an
                              error tracker, not an APM.
ACTIVITY_LOG_ENABLED        Kill switch for ActivityLog writes. Default True.
                              The log stream is unaffected either way.
ACTIVITY_RETENTION_DAYS     Horizon for prune_activity. Default 90.
```

### Optional
```
SITE_DOMAIN                 Domain written to the django.contrib.sites row.
                              Defaults to CANONICAL_HOST, then to the first
                              ALLOWED_HOSTS entry. Set it explicitly so
                              reordering ALLOWED_HOSTS cannot change it.
                              Does not affect the OAuth callback host.
EMAIL_BACKEND               Django email backend
EMAIL_HOST / PORT / etc.    SMTP config if email enabled
```

**`ALLOWED_HOSTS` must contain the hosts being redirected, not just the
canonical one.** `request.get_host()` validates the Host header and raises
`DisallowedHost`, which Django turns into a bare 400 *before*
`CanonicalHostMiddleware` can issue its redirect. Listing only
`CANONICAL_HOST` leaves `www` returning 400 rather than a 301 — the exact host
the setting exists to rescue. Verified:

```
ALLOWED_HOSTS = [wearechecking.gg]                       www -> 400
ALLOWED_HOSTS = [wearechecking.gg, www.wearechecking.gg] www -> 301
```

Railway's `healthcheck.railway.app` is the exception: `settings.py` appends it
to `ALLOWED_HOSTS` itself, so it never needs listing here. See the Healthcheck
section under Observability for why leaving it to the env var breaks deploys.

Whenever `CANONICAL_HOST` changes, add
`https://<host>/accounts/discord/login/callback/` as a redirect URL in the
Discord developer portal — the callback follows the request host.

---

## Local development setup

**Prerequisites:** Python 3.13, MySQL or MariaDB running locally,
PyCharm (recommended), Windows PowerShell.

```powershell
# Clone and set up venv
python -m venv venv
venv\Scripts\Activate.ps1

# Install dependencies
pip install -r backend/requirements.txt

# Download Tailwind binary (one time)
Invoke-WebRequest `
  -Uri "https://github.com/tailwindlabs/tailwindcss/releases/latest/download/tailwindcss-windows-x64.exe" `
  -OutFile "bin\tailwindcss.exe"

# Build CSS
.\bin\tailwindcss.exe -i backend\static\css\tailwind.css `
  -o backend\static\css\output.css

# Create local database
mysql -u root -p
> CREATE DATABASE endurance_planner CHARACTER SET utf8mb4;
> CREATE USER 'endurance_user'@'localhost' IDENTIFIED BY 'localdevpassword';
> GRANT ALL ON endurance_planner.* TO 'endurance_user'@'localhost';

# Configure environment
cp backend\.env.example backend\.env
# Edit backend\.env with your values

# Run migrations
cd backend
python manage.py migrate
python manage.py setup_discord_oauth

# Start dev server
python manage.py runserver
```

**PyCharm config:**
- Python interpreter: Settings → Project → Python Interpreter
  → Add Existing → `venv\Scripts\python.exe`
- Run configuration: Django Server, script `backend\manage.py`,
  parameter `runserver`

**Watch CSS during development:**
```powershell
make css-watch
# or
.\bin\tailwindcss.exe -i backend\static\css\tailwind.css `
  -o backend\static\css\output.css --watch
```

---

## Deployment (Railway)

**Stack:** Railpack builder, MySQL add-on, automatic deploy
on push to main branch.

**Start command** (in `railpack.json` and Railway settings):
```
python manage.py migrate --noinput &&
python manage.py setup_discord_oauth &&
python manage.py collectstatic --noinput &&
gunicorn config.wsgi:application --bind 0.0.0.0:$PORT --workers 3 --timeout 60
```

**Deploy a change:**
```powershell
# Rebuild CSS if templates changed
.\bin\tailwindcss.exe -i backend\static\css\tailwind.css `
  -o backend\static\css\output.css --minify

git add .
git commit -m "Description of change"
git push   # Railway auto-deploys
```

**Database reset** (required when resetting migrations):
1. Delete Railway MySQL service
2. Add new MySQL service
3. Update DB_* environment variables with new connection values
4. Redeploy — migrations run fresh automatically

**Discord OAuth setup for new environments:**
1. Add redirect URL in discord.com/developers:
   `https://<domain>/accounts/discord/login/callback/`
2. Set `DISCORD_CLIENT_ID` and `DISCORD_CLIENT_SECRET` in
   Railway environment variables
3. `setup_discord_oauth` management command runs on every
   deploy and configures allauth automatically

---

## Known platform quirks

**Windows development:**
- `mysqlclient` now ships Windows wheels and installs cleanly, so it is the
  driver actually used. `PyMySQL` is kept only as a fallback for environments
  where the wheel is unavailable — in practice the fallback never fires.
- `config/__init__.py` contains the driver selection and version spoof:
  ```python
  try:
      import MySQLdb
  except ImportError:
      import pymysql
      pymysql.version_info = (2, 2, 1, "final", 0)
      pymysql.install_as_MySQLdb()
  ```
- Tailwind uses `.exe` binary, not the Linux binary
- `make` requires Chocolatey — use PowerShell commands directly
  if make is unavailable

**Tailwind v4:**
- Configuration is in `tailwind.css` via `@source` directives,
  not `tailwind.config.js` (which is ignored in v4)
- Dark mode uses `@variant dark` + `[data-theme="dark"]`
- `output.css` must be committed — Railway cannot build it

**Railway:**
- App listens on `$PORT` (8080 by default) — set Railway
  networking to match
- `CSRF_TRUSTED_ORIGINS` must include `https://` scheme prefix
- Railpack is the default builder — Dockerfile is ignored
  unless Dockerfile builder is explicitly selected
- `setup_discord_oauth` command must run after every migration
  on fresh databases

**allauth 65.x:**
- Use `ACCOUNT_LOGIN_METHODS`, `ACCOUNT_SIGNUP_FIELDS`,
  `ACCOUNT_EMAIL_VERIFICATION` — the v0.x-era settings
  (`ACCOUNT_EMAIL_REQUIRED`, `ACCOUNT_AUTHENTICATION_METHOD`)
  throw critical errors
- `SOCIALACCOUNT_LOGIN_ON_GET = True` skips confirmation page
- Logout requires POST, not GET — use a form not an anchor tag

---

## Observability

Three layers, answering three different questions.

| Layer | Question it answers | Where it lives |
|---|---|---|
| Structured stdout logs | "What happened at 14:02?" | Railway log browser |
| Sentry Issues | "Did anything break, and why?" | sentry.io → Issues |
| Sentry Logs | "What else went wrong this week?" | sentry.io → Explore → Logs |
| `ActivityLog` table | "How are people using this?" | `/activity/view/` |

**Issues are exceptions; Logs are the log stream.** Out of the box one 500
produced *two* issues — the exception, and `RequestLogMiddleware`'s ERROR
line describing it — on a plan that counts them.

The fix is scoped to the one logger responsible. `RequestLogMiddleware`
writes its per-request line to `config.middleware.requests`
(`REQUEST_LOG_LOGGER`), and `build_sentry_options()` calls `ignore_logger()`
on it. `event_level` stays at the SDK default of ERROR.

Three things had to hold at once, and each is pinned by a test:

- `ignore_logger()` covers **events and breadcrumbs only** — the SDK keeps
  `_IGNORED_LOGGERS` and `_IGNORED_LOGGERS_SENTRY_LOGS` as separate sets — so
  the request line still reaches Logs, still at ERROR for a 5xx. The stdout
  stream is untouched, which matters: dropping 5xx to WARNING to dodge the
  duplicate would have broken severity in Railway, the primary destination.
- Deliberate `logger.error()` calls stay alertable. `event_level=None` would
  have silenced every one of them — `CanonicalHostMiddleware`'s
  misconfiguration error is the live example.
- The two settings compose. With `event_level=None`, setting
  `SENTRY_LOGS_LEVEL=OFF` would have left logged errors reaching Sentry by no
  path at all — neither Issues nor Logs.

The separate logger name is the load-bearing part. `ignore_logger` is
per-logger, so without it the only way to silence the request line would
also silence the configuration errors logged from the same module.

Railway's log browser looked empty before this existed because the app emitted
almost nothing — it is a log *browser*, not a log *source*. It is now fed real
structured lines, but it is still not an error tracker: retention is short,
there is no grouping, and nothing tells you an error happened. That is Sentry's
job. And neither is a usage store, which is why the activity table exists.

### The request ID

`RequestLogMiddleware` mints a 12-character ID per request and puts it in four
places: a `X-Request-ID` response header, every log line for that request, the
Sentry event's `request_id` tag, and the `ActivityLog` row. Error pages print it
as "Reference: …", and `base.html` sends the last one it saw back on the
`X-Last-Request-Id` header, so feedback arrives already correlated.

It travels in a ContextVar, which is what lets Django's own `django.request`
logger emit correlated lines without knowing this project exists.
`RequestIdFilter` falls back to `record.request` because Django logs a 4xx/5xx
from `BaseHandler.get_response` — *after* the middleware chain has unwound and
reset the ContextVar. Without that fallback the one line carrying the traceback
would be the one line missing the ID needed to find it.

### Middleware placement is load-bearing

Both logging middlewares sit high in `MIDDLEWARE`, right after
`CanonicalHostMiddleware`. Middleware is an onion and the *last* entry wraps
only the view, so a bottom-placed logger never sees a request rejected by
`CsrfViewMiddleware` — and CSRF rejections are exactly the failures worth
seeing. `request.user` and `request.htmx` are still readable because both are
inspected during the response phase, after inner middleware populated them.
`MiddlewareOrderingTests` asserts all of this.

### Admin keys must never be logged

An event admin key is a credential sitting in a URL path, so it turns up in
request lines, in `HTTP_REFERER`, and in the repr of the request object Django
attaches to its own records. `config.logging.redact_admin_key()` rewrites
`/admin/<key>/` to `/admin/[redacted]/`, and it is applied in three places:

- `JsonFormatter`, on the **serialised** line rather than per value. Django
  attaches the request *object*; only `default=str` turns it into text, so a
  per-value pass runs too early to see it.
- `request_log_context()`, covering `path` and `referer`.
- `ActivityLogMiddleware`, before the path is written to the database.

The allowlist in `_ADMIN_SUBROUTES` must list every literal segment that can
follow `/admin/` in `events/urls.py`. A missing one gets redacted and that
route becomes unreadable in the logs; a stale one is harmless. Segments
starting with `<` are preserved so route *patterns* survive intact.

`scrub_event()` is the Sentry `before_send` hook and walks the whole event
rather than named fields — secrets turn up in stack-frame locals (`admin_key`
is a view argument), breadcrumb data, and request reprs, and a field list
cannot be kept in step with SDK versions.

**`before_send` does not apply to Sentry Logs.** Logs are a separate payload
with a separate callback, so `scrub_log()` is registered as `before_send_log`
and both are required. Forwarding logs without it ships admin keys to Sentry
in the clear, because the raw arguments of a log call are sent as individual
`sentry.message.parameter.N` attributes that never pass through a logging
formatter — and `django.request` puts the failing URL there.

**What is deliberately not forwarded.** `send_default_pii=False` governs only
what the SDK collects for itself; custom attributes bypass it completely, so
it says nothing about what this app sends. `_LOG_DROP_KEYS` drops the client
IP from every forwarded log — Railway already sees it as the host, and Sentry
would be a new recipient of it on every 4xx, which sits badly next to a
`Feedback` model that deliberately stores none. The Discord username *is*
kept: it is what makes "someone said signup is broken" actionable, and
`ActivityLog` already stores it.

`scrub_log()` also **flattens non-primitive attribute values to strings**
before redacting. Sentry calls `safe_repr` on attributes *after*
`before_send_log` has run, so a live object passed through untouched gets
stringified downstream where nothing can clean it. That is exactly how
`django.request` leaks a URL: it attaches the request object itself.
Sentry only stores primitives anyway, so the coercion costs nothing.

Note that `runserver`'s own `django.server` access line is *not* redacted. It
is dev-only; gunicorn writes no access log in production.

### Action names

`events/activity.py` maps `(url_name, method)` to a stable label like
`signup.submit`. Names are declared, not derived, because a renamed URL would
otherwise split one feature's history into two buckets and break every
month-over-month comparison. Unmapped routes fall back to
`<url_name>.<method>`. `event_search` is excluded — it fires per keystroke —
along with both operator consoles, so reading the data does not change it.

Views add context with `log_detail(request, **fields)`; the middleware merges
it into the row's `detail` JSON. Note that **422 responses need no
instrumentation at all**: `status_code` alone answers "which admin form
produces the most validation errors", which is the highest-value friction
signal on the site.

### The visitor cookie

`wac_vid` holds a random uuid4 hex, httponly, SameSite=Lax, one year. It is
first-party only and stitches an anonymous visitor's actions into a trail —
without it "opened the signup form three times and never submitted" is
unmeasurable. A cookie value that is not 32 hex characters is replaced rather
than trusted.

### Failure is never the site's problem

`ActivityLogMiddleware` wraps its write in `try/except` and logs a warning.
Analytics is the least important thing this application does and must never be
the reason a page fails to render. `ActivityLogMiddlewareTests` asserts this
with a patched-to-explode manager.

`ACTIVITY_LOG_ENABLED=False` disables the writes entirely via
`MiddlewareNotUsed`; the log stream is unaffected.

### Client-side errors

Server-side logging cannot see a JavaScript failure — the page breaks while
every server signal reads 200 OK. `base.html` reports `htmx:responseError`,
`htmx:sendError`, `window.onerror` and `unhandledrejection` to
`/client-error/`, deduplicated and capped at 5 per page load so an error inside
a render loop cannot flood the endpoint. It always answers 204 — returning an
error to an error handler is how a reporting loop starts.

Two server-side limits, and the distinction matters. The per-visitor cap (20
an hour) is keyed on the `wac_vid` cookie, so a caller that discards it looks
like a new visitor every request — it bounds one stuck browser, not an
attacker. The global cap (500 an hour) is the one that actually bounds a
public, unauthenticated write endpoint, because it depends on nothing the
client supplies. Do not remove it on the grounds that the per-visitor limit
already covers it.

**422 is deliberately not reported.** It is this app's "here are your
validation errors" response, already swapped into the page; reporting it would
bury real faults in form typos.

These rows carry `action='client.error'` with a 200 status, so the dashboard
counts errors with `activity.ERROR_FILTER` rather than `status_code >= 400`. A
status-only filter would miss every JavaScript failure on the site.

The same change also fixed a UX bug: a 500 from an HTMX action used to produce
complete silence, and now raises a toast.

### Retention

```powershell
python manage.py prune_activity            # uses ACTIVITY_RETENTION_DAYS
python manage.py prune_activity --dry-run
```

Needs a Railway cron service to run on a schedule. Until one exists it is a
manual job, which at this volume is fine for a long time. A retention of 0 or
less is refused rather than silently emptying the table.

### Healthcheck

`railway.toml` points at `/healthz/`, a plain-text view with no database
access and no template. It used to point at `/`, which rendered the whole
homepage — queries and all — on every probe and would have made the activity
table mostly probe traffic. Both logging middlewares skip the path.

**The probe arrives as `Host: healthcheck.railway.app`**, and getting a 200 back
takes two things that are easy to get half right:

- `settings.py` appends that host to `ALLOWED_HOSTS` unconditionally. Leave it
  to the env var and a deploy dies as a bare 400 `DisallowedHost` — Django
  validates the Host header in `CommonMiddleware.process_request`, which runs
  on every request whatever the path, so exempting the path is not enough.
- `CanonicalHostMiddleware` skips `HEALTHCHECK_PATH`. The probe host is by
  definition not the canonical host, and a healthcheck scores a 301 exactly the
  way it scores a 400.

Get either half wrong and Railway reports only `1/1 replicas never became
healthy` while the build log shows a clean build — because the build *is*
clean. This cost one failed deploy already; `HealthcheckReachabilityTests`
covers both halves plus the full middleware chain.

---

## Stint calculation

All calculation logic lives in `events/utils.py`:

```python
stint_length_seconds(event)      # Single stint duration in seconds
last_stint_length_seconds(event) # Final stint — usually shorter
total_stints(event)              # ceil(race_length / stint_length)
total_race_laps(event)           # Planned lap count
laps_remaining_after_stint(event, n)
format_stint_duration(seconds)   # 3720 -> "62m"
seconds_to_mmss(seconds)         # 105 -> "1:45"
validate_stint_sanity(event)     # List of warning strings, never raises
get_stint_windows(event, assignment_overrides=None)
                                 # All stints with start/end/duration/is_last/
                                 #   is_overridden. The single source of truth
                                 #   for stint timing — it is the only helper
                                 #   that honours actual_start_utc overrides.
slot_grid_anchor(event)          # Wall-clock :00/:30 grid origin
get_availability_slots(event)    # All 30-min UTC slots in the event window
build_stint_availability_matrix(drivers, windows, grid_anchor)
                                 # {driver_id: {stint_num: 'full'|'partial'|
                                 #   'none'|'empty'}}
```

`normalize_iso(dt)` lives in `events/views.py`.

**Formula:**
```
stint_length = (avg_lap × target_laps) + in_lap + out_lap - (avg_lap × 2)
total_stints = ceil(race_length_seconds / stint_length)
```

Pit window logic was intentionally removed — stint length
is defined by the fuel load, so every stint end is a pit stop.

Stint 1 starts at `effective_start_datetime_utc` (race start when set,
otherwise session start). Setting `actual_start_utc` on a `StintAssignment`
overrides that stint's start and cascades to all later stints until the next
override.

There is deliberately no standalone `stint_start_time()` / `stint_end_time()`
helper. They existed, had no callers, and computed times *without* override
support — a trap for anyone who found them. Use `get_stint_windows()`.

---

## Feature flags and future work

**On the horizon (not yet implemented):**
- Discord notifications (stint reminders, signup alerts)
- Live race dashboard / "Race Control" page showing current
  stint, time remaining, driver up next
- Driver claiming — linking manually-added drivers to Discord
  accounts retroactively
- Event ownership transfer between Discord users
- Rate limiting on admin views
- A Railway cron service to run `prune_activity` on a schedule (it is a
  manual job until then)
- Splitting `events/tests.py` into a `tests/` package
- Redesigning `404.html`, `500.html` and `admin_error.html` — they still use
  generic Tailwind utilities (`rounded-lg`, default fonts) and violate the
  sharp-corners design system
- Setting `CSRF_COOKIE_SECURE`, `SECURE_HSTS_SECONDS`, `SECURE_SSL_REDIRECT`

**Deliberate omissions (by design):**
- No email auth — Discord only
- No driver account profiles or settings pages
- No maximum driver count or stint validation
- No WebSocket live updates — "refresh to update" is acceptable
- Admin and create-stints pages are desktop-only (mobile warning
  banner shown)

---

## Testing

**928 tests across 129 classes** in `backend/events/tests.py`, run against
SQLite in-memory via `config/test_settings.py`.

```powershell
cd backend
..\venv\Scripts\python.exe manage.py test --settings=config.test_settings
```

The run is clean — no tracebacks in the output. If any appear,
something has regressed.

Conventions used throughout:
- Arrange / act / assert with blank lines between the three
- One behaviour per test; the method name states the expected behaviour
- `make_event()` / `save_event()` / `utc()` factories at the top of the file
- Tests that can only be expressed against markup read the template source
  through a `_read_template()` helper rather than an inline path

`tests.py` is a single ~7k-line file. Splitting it into a `tests/` package is
known technical debt, deferred deliberately.

**Pre-deploy checks:**
```powershell
python manage.py check
python manage.py check --deploy   # Warns about HSTS / SSL redirect /
                                   # CSRF_COOKIE_SECURE — known, not yet
                                   # addressed
python manage.py makemigrations --check --dry-run
```

---

## Feedback

User feedback is stored in the `Feedback` model and viewable
at `/feedback/view/` behind the `FEEDBACK_PASSWORD` environment
variable. No email integration — DB only.

Each submission carries the `request_id` of the last request that browser saw
answered, so a report can be traced straight to its log line, its Sentry issue,
and that visitor's trail at `/activity/view/?request=<id>`.

---

## Security notes

**Admin key URL logging (residual risk)**
The admin key appears in Railway access logs once per session —
on the initial key-bearing request which results in a 302
redirect to the key-less admin URL. Subsequent admin page
visits use `/<event_id>/admin/` with session cookie auth and
are not logged with the key. Accepted residual risk: anyone
with Railway dashboard access who reads logs can extract a
key from that single redirect entry. Mitigated by: Railway
access limited to project owner, 20-character random key,
session established immediately so the key-bearing URL does
not need to be revisited.

A POST-based key submission (keeping the key out of the URL
entirely) would eliminate this residual risk but requires a
UX change to the shareable admin link. Not implemented in v0.1.

**driver_delete authorization (fixed in v0.1)**
The driver_delete endpoint previously had no authorization
check. Fixed to require either: (a) the requesting user is
the Discord-authenticated owner of the driver record, or
(b) the requesting user holds a valid admin session for the
event. CSRF protection alone is insufficient for destructive
endpoints since CSRF tokens are freely available from any
page on the site.

**Live stint time editing — open to Discord users (known)**
Any Discord-authenticated user can edit stint start times
on the view event page via the set-start / reset-start
endpoints. This is intentional for Phase 1 — the tool is
used by good-faith team members. No event ownership or
admin session is required. Noted for future tightening if
abuse occurs.

**Session fixation on admin promotion**
`_grant_admin_session()` rotates the session ID when granting admin, but only
on the transition — see the session gotchas above for why rotating on every
request logs users out.

**Login CSRF (accepted)**
`SOCIALACCOUNT_LOGIN_ON_GET = True` means a GET to
`/accounts/discord/login/` starts the OAuth flow, so a third-party page can
initiate a login. The consequence is limited to being signed into one's own
Discord account unexpectedly. Kept because the admin and my-availability
views redirect to that URL directly, and requiring a POST would add an
interstitial click to those flows.

**Activity dashboard shares the feedback password**
`/activity/view/` uses `FEEDBACK_PASSWORD` and the same session-gate pattern as
the feedback viewer. Same operator, same trust level, and a second secret would
be a second secret to rotate and leak. It carries the same caveats: no rate
limiting, no lockout.

**Admin keys are stripped from logs, Sentry and the activity table**
See the Observability section. Verified end to end against a real `sentry_sdk`
init with a capturing transport: the key appears in neither the envelope nor
the log stream. Railway's own edge logging remains outside this project's
control - that is the pre-existing residual risk documented above.

**What reaches Sentry**
Unhandled exceptions with stack traces, and — at `SENTRY_LOGS_LEVEL` and
above — log records. Admin keys are stripped from both by `scrub_event` and
`scrub_log`, which are separate hooks covering separate payloads; neither
applies to the other's. Client IPs are dropped from logs. Discord usernames
are sent deliberately. Both scrubbers **fail closed**: they do not catch
their own exceptions, because the SDK drops a payload whose hook raises, and
losing one report costs far less than shipping a credential from a scrubber
that half-finished.

**Visitor cookie**
`wac_vid` is a random first-party identifier with no cross-site scope and no
personal data. The site has no privacy page today; worth a footer line
eventually.

**Feedback viewer**
`/feedback/view/` is protected by a single shared password compared with
`hmac.compare_digest()`, with a 1-second sleep on failure. There is no rate
limiting or lockout.
