# Endurance Planner

A Django web application for sim racing teams to plan driver stints for endurance races.

---

## Prerequisites

- Python 3.11+
- MariaDB 10.6+ (or MySQL 8+)

---

## 1. Install MariaDB

### macOS (Homebrew)

```bash
brew install mariadb
brew services start mariadb
```

### Windows

Download the official installer from https://mariadb.org/download/ and run it.
During setup, set a root password and note it for the next step.

---

## 2. Create the database and user

Open a MariaDB shell (`mariadb -u root -p`) and run:

```sql
CREATE DATABASE endurance_planner CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'endurance_user'@'localhost' IDENTIFIED BY 'localdevpassword';
GRANT ALL PRIVILEGES ON endurance_planner.* TO 'endurance_user'@'localhost';
FLUSH PRIVILEGES;
```

---

## 3. Configure the environment

Copy `.env.example` to `.env` and fill in the values:

```bash
cp backend/.env.example backend/.env
```

Generate a secret key:

```bash
python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
```

Paste the output into `DJANGO_SECRET_KEY` in your `.env`.

---

## 4. Set up the Python environment

```bash
python -m venv venv

# macOS / Linux
source venv/bin/activate

# Windows
venv\Scripts\activate

pip install -r backend/requirements.txt
```

---

## 5. Run migrations

```bash
cd backend
python manage.py migrate
```

---

## 6. Start the development server

```bash
python manage.py runserver
```

Visit http://127.0.0.1:8000/

---

## 7. PyCharm configuration

### Python interpreter

1. Open **Settings → Project → Python Interpreter**
2. Click the gear icon → **Add Interpreter → Add Local Interpreter**
3. Select **Existing** and browse to:
   - macOS/Linux: `venv/bin/python`
   - Windows: `venv\Scripts\python.exe`

### Django run configuration

1. Go to **Run → Edit Configurations**
2. Click **+** → **Django Server**
3. Set:
   - **Name:** `runserver`
   - **Script path:** `backend/manage.py`
   - **Parameters:** `runserver`
   - **Working directory:** `backend/`
4. Under **Environment variables**, ensure your `.env` values are loaded
   (python-dotenv handles this automatically via `settings.py`).

---

## Project structure

```
endurance-planner/
├── venv/                   # Python virtual environment (not committed)
├── backend/
│   ├── config/             # Django project settings & URLs
│   ├── events/             # Main app: models, views, utils, forms
│   │   └── templatetags/   # Custom template filters (tz_filters)
│   ├── templates/          # HTML templates
│   │   └── partials/       # HTMX partial templates (added in later phases)
│   ├── static/css/         # Custom CSS
│   ├── .env                # Local environment variables (not committed)
│   ├── .env.example        # Template for .env (committed)
│   └── manage.py
├── .gitignore
└── README.md
```
