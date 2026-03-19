# Database Migrations

EvoClaw supports both SQLite (default) and PostgreSQL.

## Switching to PostgreSQL

1. Install: `pip install psycopg2-binary`
2. Create database: `createdb evoclaw`
3. Set env var: `DATABASE_URL=postgresql://user:pass@localhost:5432/evoclaw`
4. Run migration: `python -m host.migrations.sqlite_to_pg`

## Schema

The schema is defined in `host/db.py:init_database()`. It is automatically created on first run.
