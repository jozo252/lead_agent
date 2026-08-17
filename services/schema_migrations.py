from sqlalchemy import inspect, text

from extensions import db


COMPANY_COLUMNS = {
    "sk_nace_code": "VARCHAR(20)",
    "sk_nace_name": "VARCHAR(500)",
    "employee_count": "INTEGER",
    "employee_count_source": "VARCHAR(100)",
    "company_type": "VARCHAR(255)",
    "services": "JSON",
    "markets": "JSON",
    "works_abroad": "BOOLEAN",
    "regions": "JSON",
    "subcontractor_need": "VARCHAR(20)",
    "outreach_relevant": "BOOLEAN",
    "analysis_reason": "TEXT",
    "analysis_evidence": "JSON",
    "website_analyzed_at": "DATETIME",
    "contacts_checked_at": "DATETIME",
}

SYNC_STATE_COLUMNS = {
    "fetched_records": "INTEGER NOT NULL DEFAULT 0",
    "skipped_records": "INTEGER NOT NULL DEFAULT 0",
}

LEAD_COLUMNS = {
    "company_id": "INTEGER",
}

EMAIL_REPLY_COLUMNS = {
    "imap_message_id": "VARCHAR(255)",
}


def ensure_company_columns():
    """Doplní nové nullable stĺpce do existujúcej lokálnej SQLite databázy."""
    inspector = inspect(db.engine)
    existing_columns = {
        column["name"]
        for column in inspector.get_columns("companies")
    }

    missing_columns = {
        name: column_type
        for name, column_type in COMPANY_COLUMNS.items()
        if name not in existing_columns
    }

    if not missing_columns:
        return

    with db.engine.begin() as connection:
        for name, column_type in missing_columns.items():
            connection.execute(
                text(f"ALTER TABLE companies ADD COLUMN {name} {column_type}")
            )


def ensure_sync_state_columns():
    """Doplní počítadlá pre veľké a obnoviteľné RPO importy."""
    inspector = inspect(db.engine)
    existing_columns = {
        column["name"]
        for column in inspector.get_columns("sync_states")
    }

    with db.engine.begin() as connection:
        for name, column_type in SYNC_STATE_COLUMNS.items():
            if name not in existing_columns:
                connection.execute(
                    text(
                        "ALTER TABLE sync_states "
                        f"ADD COLUMN {name} {column_type}"
                    )
                )


def ensure_lead_columns():
    """Doplní prepojenie existujúcich CRM leadov na RPO firmy."""
    inspector = inspect(db.engine)
    existing_columns = {
        column["name"]
        for column in inspector.get_columns("lead")
    }

    with db.engine.begin() as connection:
        for name, column_type in LEAD_COLUMNS.items():
            if name not in existing_columns:
                connection.execute(
                    text(f"ALTER TABLE lead ADD COLUMN {name} {column_type}")
                )


def ensure_email_reply_columns():
    """Doplní identifikátor IMAP odpovede pre ochranu pred duplicitami."""
    inspector = inspect(db.engine)
    existing_columns = {
        column["name"]
        for column in inspector.get_columns("email_reply")
    }

    with db.engine.begin() as connection:
        for name, column_type in EMAIL_REPLY_COLUMNS.items():
            if name not in existing_columns:
                connection.execute(
                    text(
                        "ALTER TABLE email_reply "
                        f"ADD COLUMN {name} {column_type}"
                    )
                )
