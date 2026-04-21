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
| Framework | Django 5.x | `AUTH_USER_MODEL = 'events.User'` |
| Database | MySQL (Railway) / MariaDB (local) | PyMySQL driver on Windows |
| Interactivity | HTMX 2.x | Server-driven partial updates |
| Reactivity | Alpine.js 3.x | Client-side state, no build step |
| CSS | Tailwind CSS v4 | CLI binary, no Node required |
| Auth | django-allauth 0.65+ | Discord OAuth only |
| Static files | Whitenoise | Served from gunicorn directly |
| Deployment | Railway (Railpack) | MySQL add-on, auto-deploy on push |
| Domain | wearechecking.gg | Namecheap, CNAME to Railway |

---

## Project structure

```
endurance-planner/
├── backend/                    # Django project root
│   ├── config/
│   │   ├── settings.py         # All configuration
│   │   ├── urls.py             # Root URL config
│   │   └── wsgi.py
│   ├── events/                 # Main app — all models and views
│   │   ├── models.py           # User, Event, Driver, Availability,
│   │   │                       #   StintAssignment, Feedback
│   │   ├── views.py            # All views
│   │   ├── urls.py             # All URL patterns
│   │   ├── forms.py            # EventCreateForm
│   │   ├── utils.py            # Stint calculations
│   │   ├── adapters.py         # Discord OAuth adapter
│   │   ├── context_processors.py  # discord_user in all templates
│   │   ├── templatetags/
│   │   │   └── tz_filters.py   # to_tz, time_in_tz, to_utc_z,
│   │   │                       #   seconds_to_mmss, seconds_to_hours_display,
│   │   │                       #   dict_get
│   │   └── management/
│   │       └── commands/
│   │           └── setup_discord_oauth.py
│   ├── templates/
│   │   ├── base.html           # Fixed header, footer, bg-grid,
│   │   │                       #   feedback widget, theme toggle
│   │   ├── home.html
│   │   ├── event_create.html
│   │   ├── signup.html
│   │   ├── signup_edit.html
│   │   ├── signup_success.html
│   │   ├── admin.html          # Combines event details + stint
│   │   │                       #   assignment (no separate page)
│   │   ├── view.html
│   │   ├── feedback_view.html
│   │   ├── admin_error.html
│   │   ├── 404.html
│   │   ├── 500.html
│   │   └── partials/           # HTMX swap targets
│   │       ├── signup_form.html
│   │       ├── driver_list.html
│   │       ├── driver_row.html
│   │       ├── driver_name_display.html
│   │       ├── driver_name_edit_form.html
│   │       ├── admin_add_driver.html
│   │       ├── availability_grid.html
│   │       ├── search_results.html
│   │       ├── admin_details_errors.html
│   │       └── admin_calc_errors.html
│   ├── static/
│   │   └── css/
│   │       ├── tailwind.css    # Source — @source directives,
│   │       │                   #   all tokens and component classes
│   │       └── output.css      # Compiled — committed to git
│   ├── railpack.json           # Railpack build config
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
id                UUIDField     # Primary key, auto-generated
admin_key         CharField(20) # Random string, used in admin URL
name              CharField
team_name         CharField     # Optional
game              CharField     # Optional — iRacing, LMU, ACC etc.
date              DateField
start_time_utc    TimeField
length_seconds    IntegerField
car               CharField     # Optional
track             CharField     # Optional
setup             TextField     # Optional
fuel_capacity     FloatField    # Optional — for stint calc
fuel_per_lap      FloatField    # Optional
target_laps       IntegerField  # Optional
avg_lap_seconds   FloatField    # Optional
in_lap_seconds    FloatField    # Optional
out_lap_seconds   FloatField    # Optional
recruiting        BooleanField  # Show on home page
created_by        FK(User)      # Nullable — Discord user who created
```

Key properties: `start_datetime_utc`, `end_datetime_utc`,
`has_required_stint_fields`

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
event         FK(Event)
stint_number  IntegerField  # 1-indexed
driver        FK(Driver)    # Nullable — unassigned stints allowed
```
Unique together: `(event, stint_number)`

### Feedback
```python
text          TextField
page_url      CharField
user_agent    CharField
ip_address    GenericIPAddressField  # Nullable
submitted_at  DateTimeField
```

---

## URL structure

```
/                                           home
/create/                                    event creation
/<event_id>/view/                           public view page
/<event_id>/signup/                         driver signup
/<event_id>/signup/<driver_id>/edit/        edit availability (URL key)
/<event_id>/signup/<driver_id>/delete/      remove driver
/<event_id>/signup/<driver_id>/success/     post-signup success
/<event_id>/my-availability/               edit availability (Discord)
/<event_id>/admin/<admin_key>/             admin page (key auth)
/<event_id>/admin/                          admin page (Discord auth)
/<event_id>/admin/save-details/            save event detail fields
/<event_id>/admin/save-calc/               save stint calc fields
/<event_id>/admin/save-assignments/        save stint assignments
/<event_id>/admin/add-driver/              add driver manually
/<event_id>/admin/remove-driver/<id>/      remove driver
/<event_id>/admin/edit-driver/<id>/        edit driver name
/<event_id>/admin/create-stints/           redirects to admin page
/search/                                    event search (HTMX)
/lookup/                                    removed — was UUID lookup
/feedback/submit/                           feedback form POST
/feedback/view/                            password-protected viewer
/accounts/                                  allauth URLs (Discord OAuth)
/set-timezone/                              set admin_timezone cookie
```

---

## Authentication model

Three parallel auth mechanisms coexist:

**1. Discord OAuth (recommended)**
- Login via `/accounts/discord/login/` → Discord → callback
- Sets Django session, populates `User` model
- Admin access: `event.created_by == request.user`
- Driver access: `driver.user == request.user`
- 30-day rolling session (`SESSION_COOKIE_AGE = 2592000`)

**2. Admin key URL (legacy / fallback)**
- Admin key embedded in URL: `/<event_id>/admin/<admin_key>/`
- Validated with `hmac.compare_digest()` for timing safety
- Sets session key `admin_{event_id} = True` on valid access
- Sub-routes use `require_admin_session()` helper

**3. Edit URL (drivers without Discord)**
- Driver edit URL contains driver UUID
- No additional auth — URL possession = access
- Works for manually-added drivers with no Discord account

---

## Key implementation patterns

### HTMX partial updates
HTMX handles form submissions, inline field editing, driver
removal, and search. The `django-htmx` middleware provides
`request.htmx` boolean. Views return full pages on direct
load and HTML fragments on HTMX requests.

Toast notifications use `HX-Trigger: showToast` response
header caught by an Alpine `@show-toast.window` listener.

### Alpine.js reactivity
Alpine handles:
- Dark/light theme toggling (`data-theme` attribute on `<html>`)
- Stint assignment table state (`stintAssignment()` component)
- Stint calculation live preview (`stintCalc()` component)
- Timezone picker in signup forms
- Login modal (`$dispatch('open-login')`)
- Feedback widget
- Copy-to-clipboard buttons

### Timezone handling
All times stored in UTC. Client-side conversion via
`Intl.DateTimeFormat` API. ISO strings normalized with Z suffix
using `normalize_iso()` helper for consistent JS comparison.
Template filter `to_utc_z` formats datetimes for Alpine
consumption.

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
DJANGO_SECRET_KEY           Strong random key
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
DISCORD_CLIENT_ID           From discord.com/developers
DISCORD_CLIENT_SECRET       From discord.com/developers
FEEDBACK_PASSWORD           Password for /feedback/view/
```

### Optional
```
EMAIL_BACKEND               Django email backend
EMAIL_HOST / PORT / etc.    SMTP config if email enabled
```

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
- Use PyMySQL instead of mysqlclient (C build deps unavailable)
- `config/__init__.py` contains version spoof:
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

**allauth v0.65+:**
- Use `ACCOUNT_LOGIN_METHODS`, `ACCOUNT_SIGNUP_FIELDS`,
  `ACCOUNT_EMAIL_VERIFICATION` — the v0.x-era settings
  (`ACCOUNT_EMAIL_REQUIRED`, `ACCOUNT_AUTHENTICATION_METHOD`)
  throw critical errors
- `SOCIALACCOUNT_LOGIN_ON_GET = True` skips confirmation page
- Logout requires POST, not GET — use a form not an anchor tag

---

## Stint calculation

All calculation logic lives in `events/utils.py`:

```python
stint_length_seconds(event)     # Single stint duration in seconds
total_stints(event)             # ceil(race_length / stint_length)
stint_start_time(event, n)      # UTC datetime for stint n (1-indexed)
stint_end_time(event, n)        # UTC datetime for stint n end
get_stint_windows(event)        # List of {stint_number, start_utc, end_utc}
get_availability_slots(event)   # All 30-min UTC slots in event window
build_stint_availability_matrix(drivers, windows)
                                # {driver_id: {stint_num: 'full'|'partial'|'none'}}
normalize_iso(dt)               # Format UTC datetime as ISO with Z suffix
```

**Formula:**
```
stint_length = (avg_lap × target_laps) + in_lap + out_lap - (avg_lap × 2)
total_stints = ceil(race_length_seconds / stint_length)
```

Pit window logic was intentionally removed — stint length
is defined by the fuel load, so every stint end is a pit stop.

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

**Deliberate omissions (by design):**
- No email auth — Discord only
- No driver account profiles or settings pages
- No maximum driver count or stint validation
- No WebSocket live updates — "refresh to update" is acceptable
- Admin and create-stints pages are desktop-only (mobile warning
  banner shown)

---

## Testing

No automated test suite exists in v0.1.0. Manual verification
checklist approach used throughout development. When adding
tests in future, Django's `TestCase` with SQLite in-memory
database is the standard path.

**Pre-deploy manual checks:**
```powershell
python manage.py check
python manage.py check --deploy  # Will warn about HTTPS settings —
                                  # expected, handled by Railway proxy
```

---

## Feedback

User feedback is stored in the `Feedback` model and viewable
at `/feedback/view/` behind the `FEEDBACK_PASSWORD` environment
variable. No email integration — DB only.
