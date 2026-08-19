# Database Migrations

Database schema changes are managed only through Flask-Migrate/Alembic. The
application no longer creates or alters tables during startup.

## New database

Set `DATABASE_URL` when a non-default database is required, then apply every
migration before starting the application:

```powershell
python -m flask --app app db upgrade
python app.py
```

Radius filtre potrebujú jednorazovo načítať bezplatné GeoNames PSČ dáta:

```powershell
python -m flask --app app import-postal-locations
```

Príkaz je idempotentný a pri ďalšom spustení existujúce lokality obnoví.

Without `DATABASE_URL`, SQLite uses `instance/leads.db`.

## Existing unversioned database

Back up the database and stop all application processes first. The original
database contains the legacy `lead`, `lead_activity`, and `email_reply` tables,
so mark that baseline before applying the RPO/outreach migration:

```powershell
Copy-Item instance\leads.db instance\leads.backup.db
python -m flask --app app db stamp ca786b56f464
python -m flask --app app db upgrade
python -m flask --app app db check
```

Do not use `stamp` on a blank database; it records a revision without creating
tables. The unused legacy `user` table is deliberately preserved and excluded
from schema comparison so existing account data is not destroyed.

## Creating a schema change

Edit `models.py`, generate a revision, and review both directions before use:

```powershell
python -m flask --app app db migrate -m "describe schema change"
python -m flask --app app db upgrade
python -m flask --app app db check
```

Test upgrades on a copy of real data. Add data backfills explicitly to the
revision and avoid destructive downgrades unless data loss is intended.
