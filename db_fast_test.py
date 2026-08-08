import argparse
from pprint import pprint
from types import SimpleNamespace

from app import app
from extensions import db
from models import Company
from services.rpo_sync import (
    aggregate_company_contacts,
    build_company_search_queries,
    deduplicate_search_results,
    fetch_search_results,
    filter_search_results,
    normalize_search_results,
    save_best_company_contacts,
    select_best_company_contacts,
)


companies = Company.query.all()
for company in companies:

    print(company)
