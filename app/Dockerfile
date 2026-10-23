FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MPLCONFIGDIR=/tmp/matplotlib \
    FLASK_APP=finance \
    DATABASE_PATH=/data/finance.db \
    BACKUP_DIR=/data/backups

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY finance ./finance
# household.json is private and git-ignored; without it the example is used.
COPY household*.json ./

# /data is the database volume; created here so a fresh named volume
# inherits the non-root owner.
RUN useradd --uid 10001 --create-home app && mkdir -p /data && chown app:app /data
USER app

EXPOSE 8000
# --preload: run migrations once in the master before the workers fork.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "4", "--preload", \
     "--access-logfile", "-", "finance:create_app()"]
