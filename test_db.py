from app import create_app
from extensions import db
from models import Company, CompanySource, CompanyActivity
from services.rpo_sync import upsert_company

app = create_app()

record = {
    "id": 18075205,
    "data": {
        "fullNames": [
            {
                "value": "Firma ABC s.r.o.",
                "validFrom": "2020-01-01",
            }
        ],
        "identifiers": [
            {
                "value": "12345678",
                "validFrom": "2020-01-01",
            }
        ],
        "activities": [
            {
                "economicActivityDescription": "Elektroinštalácie",
                "validFrom": "2020-01-01",
            },
            {
                "economicActivityDescription": "Montáž rozvádzačov",
                "validFrom": "2022-05-01",
            },
        ],
    },
}

with app.app_context():

    # vyčisti testovaciu DB
    CompanyActivity.query.delete()
    CompanySource.query.delete()
    Company.query.delete()
    db.session.commit()

    print("=== Pred importom ===")
    print("Companies:", Company.query.count())
    print("Sources:", CompanySource.query.count())
    print("Activities:", CompanyActivity.query.count())

    company = upsert_company(record)
    db.session.commit()

    print("\n=== Po prvom importe ===")
    print("Companies:", Company.query.count())
    print("Sources:", CompanySource.query.count())
    print("Activities:", CompanyActivity.query.count())

    print("\nAktivity firmy:")
    for activity in company.activities:
        print("-", activity.description)

    # druhý import rovnakého recordu
    company = upsert_company(record)
    db.session.commit()

    print("\n=== Po druhom importe ===")
    print("Companies:", Company.query.count())
    print("Sources:", CompanySource.query.count())
    print("Activities:", CompanyActivity.query.count())

    print("\nAktivity firmy:")
    for activity in company.activities:
        print("-", activity.description)