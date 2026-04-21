# WeAreChecking

A web application for planning driver stints in endurance racing events.

## Tech stack

- **Backend:** Django 6.x
- **Database:** MariaDB 11
- **Frontend:** HTMX, Alpine.js, Tailwind CSS
- **Auth:** Discord OAuth via django-allauth
- **Deployment:** Railway (primary) / Docker (self-hosted)

---

## Local development setup

### Prerequisites

- Python 3.12+
- MariaDB running locally
- `make` (for Tailwind CSS builds)
  - macOS/Linux: already available or `brew install make`
  - Windows: install via `choco install make`, `scoop install make`, or use [Git Bash](https://git-scm.com/downloads) which includes Make

### First-time setup

**1. Clone and create a virtual environment**

```bash
python -m venv venv
source venv/bin/activate   # macOS/Linux
venv\Scripts\activate      # Windows
```

**2. Install Python dependencies**

```bash
pip install -r backend/requirements.txt
```

**3. Download the Tailwind CLI binary**

macOS (Apple Silicon):
```bash
mkdir -p bin
curl -sLo bin/tailwindcss-macos \
  https://github.com/tailwindlabs/tailwindcss/releases/latest/download/tailwindcss-macos-arm64
chmod +x bin/tailwindcss-macos
```

Linux x64:
```bash
mkdir -p bin
curl -sLo bin/tailwindcss \
  https://github.com/tailwindlabs/tailwindcss/releases/latest/download/tailwindcss-linux-x64
chmod +x bin/tailwindcss
```

Windows x64 (PowerShell):
```powershell
mkdir -Force bin
Invoke-WebRequest `
  -Uri https://github.com/tailwindlabs/tailwindcss/releases/latest/download/tailwindcss-windows-x64.exe `
  -OutFile bin\tailwindcss.exe
```

The Makefile detects Windows automatically and uses `bin\tailwindcss.exe`.

**4. Build the CSS**

```bash
make css
```

**5. Set up the database**

MariaDB must be running. Run the setup script:
```bash
mariadb -u root -p < docs/create_db.sql
```

Windows (PowerShell or cmd — the `<` redirect works in cmd; use Git Bash for the above):
```powershell
Get-Content docs\create_db.sql | mariadb -u root -p
```

Or run the SQL commands in `docs/create_db.sql` manually in any client.

**6. Configure environment**

```bash
cp backend/.env.example backend/.env
# Edit backend/.env
```

At minimum, set `DJANGO_SECRET_KEY` and `DB_PASSWORD`. For Discord login to work locally, also set `DISCORD_CLIENT_ID` and `DISCORD_CLIENT_SECRET` (see step 8).

**7. Run migrations**

```bash
cd backend && python manage.py migrate
```

**8. Set up Discord OAuth**

Register an application at [discord.com/developers](https://discord.com/developers/applications):
- Add a redirect URI: `http://localhost:8000/accounts/discord/callback/`
- Copy the Client ID and Client Secret into `backend/.env`

Then seed the OAuth app record into the database:
```bash
cd backend && python manage.py setup_discord_oauth
```

Re-run this command whenever credentials change. It is idempotent.

**9. Start the dev server**

```bash
cd backend && python manage.py runserver
```

**10. Watch for CSS changes** (separate terminal)

```bash
make css-watch
```

This recompiles `output.css` whenever a template changes. You must run this
(or `make css`) whenever adding new Tailwind classes — the Play CDN is no longer used.

Before committing, run `make css` to generate a minified production build.

---

### PyCharm configuration

- Open the `endurance-planner/` root directory as the project
- **Settings → Project → Python Interpreter → Add → Existing:**
  - macOS/Linux: `venv/bin/python`
  - Windows: `venv\Scripts\python.exe`
- **Run → Edit Configurations → + → Django Server:**
  - Script path: `backend/manage.py`
  - Working directory: `backend/`

---

## Production deployment

### Railway (primary)

The app is configured for Railway via `railpack.json` and `railway.toml`.

Set these environment variables in the Railway dashboard:

| Variable | Description |
|---|---|
| `DJANGO_SECRET_KEY` | Generate with `python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"` |
| `DJANGO_DEBUG` | `False` |
| `ALLOWED_HOSTS` | Your Railway domain, e.g. `yourapp.up.railway.app` |
| `DB_NAME` | Database name |
| `DB_USER` | Database user |
| `DB_PASSWORD` | Database password |
| `DB_HOST` | Railway internal DB hostname |
| `DB_PORT` | `3306` |
| `DISCORD_CLIENT_ID` | From Discord developer portal |
| `DISCORD_CLIENT_SECRET` | From Discord developer portal |
| `FEEDBACK_PASSWORD` | Password to access `/feedback/view/` |

The deploy start command (in `railpack.json`) runs migrations, sets up Discord OAuth, collects static files, and starts gunicorn automatically.

In the Discord developer portal, add a redirect URI for your Railway domain:
`https://yourapp.up.railway.app/accounts/discord/callback/`

### Docker (self-hosted)

**1. Copy and fill in the production environment file**

```bash
cp .env.production.example .env.production
# Edit .env.production — never commit this file
```

**2. Download the Linux Tailwind binary** (if not already in `bin/`)

```bash
mkdir -p bin
curl -sLo bin/tailwindcss \
  https://github.com/tailwindlabs/tailwindcss/releases/latest/download/tailwindcss-linux-x64
chmod +x bin/tailwindcss
```

**3. Build and start**

```bash
docker-compose --env-file .env.production up -d --build
```

The app will be available on port 8000. Put nginx or a reverse proxy in front for HTTPS.

After first deploy, seed the Discord OAuth record:
```bash
docker-compose exec web python manage.py setup_discord_oauth
```

---

## URL structure

### User-facing

| URL | Description |
|---|---|
| `/` | Home — event search, create, recruiting list, My Events |
| `/create/` | Create a new event |
| `/<event_id>/view/` | View event and stint schedule |
| `/<event_id>/signup/` | Driver signup form |
| `/<event_id>/signup/<driver_id>/edit/` | Edit driver availability |
| `/<event_id>/signup/<driver_id>/success/` | Post-signup confirmation |
| `/<event_id>/my-availability/` | Driver's own stint view (requires Discord login) |

### Admin

| URL | Description |
|---|---|
| `/<event_id>/admin/<admin_key>/` | Entry point — validates key and sets session |
| `/<event_id>/admin/` | Admin dashboard (requires session) |

### Auth

| URL | Description |
|---|---|
| `/accounts/discord/login/` | Initiate Discord OAuth |
| `/accounts/discord/callback/` | OAuth callback (handled by allauth) |
| `/accounts/logout/` | Log out |

### Internal

| URL | Description |
|---|---|
| `/search/` | HTMX event search endpoint |
| `/feedback/submit/` | HTMX feedback submission |
| `/feedback/view/` | Feedback viewer (password protected) |

---

## Project structure

```
endurance-planner/
├── backend/
│   ├── config/                 # Django project settings and URLs
│   ├── events/                 # Main app
│   │   ├── adapters.py         # Discord OAuth adapter (avatar, username sync)
│   │   ├── models.py           # Event, Driver, Stint, Feedback models
│   │   ├── views.py            # All views
│   │   ├── utils.py            # Stint calculation logic
│   │   ├── templatetags/       # Custom template filters (tz_filters)
│   │   └── management/
│   │       └── commands/
│   │           └── setup_discord_oauth.py  # Seeds OAuth app record
│   ├── templates/
│   │   ├── base.html           # Site shell, header, footer, feedback widget
│   │   ├── home.html           # Homepage with login modal
│   │   ├── socialaccount/
│   │   │   └── login.html      # Styled fallback for direct /accounts/discord/login/ access
│   │   └── partials/           # HTMX swap targets
│   └── static/
│       └── css/
│           ├── tailwind.css    # Tailwind input (source)
│           └── output.css      # Compiled CSS (git-ignored, generated by make css)
├── bin/                        # Tailwind CLI binaries (git-ignored)
├── design/                     # Static design mockups and design system reference
│   └── DESIGN_SYSTEM.md
├── docs/
│   └── create_db.sql           # Local database setup script
├── docker-compose.yml
├── Makefile
├── railpack.json               # Railway build/deploy config
├── railway.toml                # Railway health check config
├── tailwind.config.js
├── .env.production.example
└── README.md
```
