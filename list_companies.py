from app import app
from models import Company


def main():
    with app.app_context():
        companies = Company.query.order_by(Company.ico).all()

        print(f"Počet firiem: {len(companies)}")

        for company in companies:
            print(company.ico or "<bez IČO>")
            print(f"  Názov: {company.official_name}")
            print(f"  kontakt: {company.contacts[0].value if company.contacts else '<bez kontaktu>'}")


if __name__ == "__main__":
    main()
