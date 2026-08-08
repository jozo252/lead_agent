import argparse
import time
from pprint import pprint
from types import SimpleNamespace

from app import app
from extensions import db
from models import Company
from services.rpo_sync import (
    aggregate_company_contacts,
    build_company_search_queries,
    deduplicate_search_results,
    fetch_derived_website_result,
    fetch_search_results,
    filter_search_results,
    has_trusted_website_result,
    normalize_search_results,
    save_best_company_contacts,
    select_best_company_contacts,
    validate_company_website_results,
)


def load_test_company(ico, official_name, municipality):
    company = Company.query.filter_by(ico=ico).first()

    if company is not None:
        return company

    if official_name:
        return SimpleNamespace(
            ico=ico,
            official_name=official_name,
            municipality=municipality,
        )

    raise RuntimeError(
        f"Firma s IČO {ico} nie je v databáze. "
        "Zadaj --name pre samostatný test alebo najprv spusti RPO synchronizáciu."
    )


def print_company_data(company):
    fields = {
        column.name: getattr(company, column.name)
        for column in Company.__table__.columns
    }
    stored_fields = {
        name: value
        for name, value in fields.items()
        if value is not None and value != ""
    }
    missing_fields = [
        name
        for name, value in fields.items()
        if value is None or value == ""
    ]

    print("\n=== Uložené polia firmy ===")
    pprint(stored_fields)
    print("\n=== Nevyplnené polia firmy ===")
    pprint(missing_fields)
    print("\n=== Uložené kontakty ===")
    pprint([
        {
            "type": contact.contact_type,
            "value": contact.value,
            "primary": contact.is_primary,
            "verified": contact.is_verified,
            "confidence": contact.confidence_score,
            "source": contact.source_url,
        }
        for contact in company.contacts
    ])
    print("\n=== Uložené aktivity ===")
    pprint([
        {
            "description": activity.description,
            "valid_from": activity.valid_from,
            "valid_to": activity.valid_to,
        }
        for activity in company.activities
    ])
    print("\n=== RPO zdroje vrátane pôvodných dát ===")
    pprint([
        {
            "external_id": source.external_id,
            "source_id": source.source_id,
            "resource_url": source.resource_url,
            "fetched_at": source.fetched_at,
            "raw_data": source.raw_data,
        }
        for source in company.sources
    ])


def fetch_company_results(company, queries, delay_seconds):
    results = []

    for index, query in enumerate(queries):
        raw_results = fetch_search_results(query)
        results.extend(normalize_search_results(raw_results))

        if delay_seconds and index < len(queries) - 1:
            time.sleep(delay_seconds)

    results = filter_search_results(deduplicate_search_results(results))
    results = validate_company_website_results(company, results)

    if not has_trusted_website_result(company, results):
        derived_result = fetch_derived_website_result(company)

        if derived_result:
            results.append(derived_result)

    return results


def print_contacts(company, aggregated, selected):
    print(f"\n{'=' * 70}")
    print(f"IČO: {company.ico} | Firma: {company.official_name}")
    print(f"Obec: {company.municipality or '-'}")
    print("=== Overené kontakty ===")
    pprint({
        "websites": aggregated["websites"],
        "emails": aggregated["emails"],
        "phones": aggregated["phones"],
    })
    print("=== Kandidátne kontakty (neukladajú sa automaticky) ===")
    pprint(aggregated["possible_contacts"])
    print("=== Vybrané na automatické uloženie ===")
    pprint(selected)


def process_company(company, queries, delay_seconds):
    results = fetch_company_results(company, queries, delay_seconds)
    aggregated = aggregate_company_contacts(company, results)
    selected = select_best_company_contacts(
        aggregated,
        include_candidates=True,
    )
    print_contacts(company, aggregated, selected)
    return aggregated


def main():
    parser = argparse.ArgumentParser(
        description="Test vyhľadania a filtrovania firemných kontaktov cez Brave.",
    )
    target_group = parser.add_mutually_exclusive_group(required=True)
    target_group.add_argument(
        "--ico",
        help="IČO jednej testovanej firmy.",
    )
    target_group.add_argument(
        "--all",
        action="store_true",
        help="Prejde všetky firmy uložené v databáze.",
    )
    parser.add_argument(
        "--name",
        help="Názov firmy použitý pri teste jednej firmy bez --save.",
    )
    parser.add_argument(
        "--municipality",
        help="Obec firmy použitá pri teste jednej firmy bez --save.",
    )
    parser.add_argument(
        "--query",
        action="append",
        help="Vlastný Brave dotaz; môžeš ho zadať viackrát.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Maximálny počet firiem pri --all.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.5,
        help="Pauza medzi Brave dotazmi v sekundách.",
    )
    parser.add_argument(
        "--save",
        action="store_true",
        help="Uloží najlepšie kontakty vrátane neoverených kandidátov do databázy.",
    )
    parser.add_argument(
        "--show-company",
        action="store_true",
        help="Vypíše všetky uložené dáta jednej firmy bez Brave vyhľadávania.",
    )
    args = parser.parse_args()

    if args.limit is not None and args.limit <= 0:
        parser.error("--limit musí byť kladné číslo.")

    if args.delay < 0:
        parser.error("--delay nemôže byť záporný.")

    #if args.all and args.save:
       # parser.error("--all nepodporuje --save; najprv si výsledky skontroluj.")

    if args.all and args.show_company:
        parser.error("--show-company funguje iba spolu s --ico.")

    if args.all and args.query:
        parser.error("Pri --all nepoužívaj --query; dotazy sa vytvoria pre každú firmu.")

    with app.app_context():
        if args.all:
            query = Company.query.order_by(Company.ico)

            if args.limit is not None:
                query = query.limit(args.limit)

            companies = query.all()
            print(f"Spracúvam {len(companies)} firiem.")
            saved_count = 0

            for company in companies:
                queries = build_company_search_queries(company)
                aggregated = process_company(company, queries, args.delay)

                if args.save:
                    saved_contacts = save_best_company_contacts(
                        company,
                        aggregated,
                        include_candidates=True,
                    )
                    db.session.commit()
                    saved_count += len(saved_contacts)

            if args.save:
                print(f"\nUložených kontaktov: {saved_count}")

            return

        company = load_test_company(
            ico=args.ico,
            official_name=args.name,
            municipality=args.municipality,
        )

        if args.show_company:
            print_company_data(company)
            return

        queries = args.query or build_company_search_queries(company)
        print("\n=== Použité dotazy ===")
        pprint(queries)
        aggregated = process_company(company, queries, args.delay)

        if not args.save:
            print("\nKontakty neboli uložené. Pre uloženie spusti skript s --save.")
            return

        saved_contacts = save_best_company_contacts(company, aggregated)
        db.session.commit()

        print("\n=== Uložené kontakty ===")
        for contact in saved_contacts:
            print(
                f"{contact.contact_type}: {contact.value} "
                f"(confidence={contact.confidence_score}, primary={contact.is_primary})"
            )


if __name__ == "__main__":
    main()
