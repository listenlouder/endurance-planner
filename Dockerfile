FROM python:3.12-slim

# System deps for mysqlclient
RUN apt-get update && apt-get install -y \
    gcc \
    pkg-config \
    default-libmysqlclient-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python dependencies
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code
COPY backend/ .

# Tailwind CSS build
COPY tailwind.config.js /app/tailwind.config.js
COPY bin/tailwindcss /usr/local/bin/tailwindcss
RUN chmod +x /usr/local/bin/tailwindcss
RUN tailwindcss \
    -i static/css/tailwind.css \
    -o static/css/output.css \
    --minify

# Collect static files
RUN python manage.py collectstatic --noinput

EXPOSE 8000

CMD ["gunicorn", "config.wsgi:application", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "3", \
     "--timeout", "60"]
