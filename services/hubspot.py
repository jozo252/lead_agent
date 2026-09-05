from datetime import datetime, timezone
from urllib.parse import quote, urlparse

import requests
from flask import current_app


class HubSpotError(RuntimeError):
    pass


def _request(method, path, *, json=None, params=None, allow_not_found=False):
    token = current_app.config.get("HUBSPOT_ACCESS_TOKEN")
    if not token:
        raise HubSpotError("Chýba HUBSPOT_ACCESS_TOKEN.")

    try:
        response = requests.request(
            method,
            f"{current_app.config['HUBSPOT_API_BASE']}{path}",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=json,
            params=params,
            timeout=20,
        )
    except requests.RequestException as exc:
        raise HubSpotError(f"HubSpot API nie je dostupné: {exc}") from exc

    if allow_not_found and response.status_code == 404:
        return None
    if not response.ok:
        detail = response.text.strip()[:500]
        raise HubSpotError(f"HubSpot API vrátilo HTTP {response.status_code}: {detail}")
    if response.status_code == 204 or not response.content:
        return {}
    try:
        return response.json()
    except ValueError as exc:
        raise HubSpotError("HubSpot API nevrátilo platný JSON.") from exc


def _website_domain(company):
    websites = [
        contact.value
        for contact in company.contacts
        if contact.contact_type == "website" and contact.value
    ]
    for website in websites:
        parsed = urlparse(website if "://" in website else f"https://{website}")
        domain = (parsed.hostname or "").lower()
        if domain.startswith("www."):
            domain = domain[4:]
        if domain:
            return domain
    return None


def _company_properties(lead):
    company = lead.company if hasattr(lead, "company") else None
    properties = {"name": lead.company_name}
    if company is None:
        return properties

    domain = _website_domain(company)
    values = {
        "domain": domain,
        "city": company.municipality,
        "zip": company.postal_code,
        "address": company.street,
    }
    properties.update({key: value for key, value in values.items() if value})
    return properties


def _contact_properties(lead):
    values = {
        "email": lead.email,
        "phone": lead.phone,
        "company": lead.company_name,
    }
    return {key: value for key, value in values.items() if value}


def _upsert_contact(lead):
    properties = _contact_properties(lead)
    if not properties.get("email"):
        raise HubSpotError("Lead nemá e-mail pre HubSpot kontakt.")

    if lead.hubspot_contact_id:
        result = _request(
            "PATCH",
            f"/crm/v3/objects/contacts/{lead.hubspot_contact_id}",
            json={"properties": properties},
        )
        return str(result["id"])

    existing = _request(
        "GET",
        f"/crm/v3/objects/contacts/{quote(properties['email'], safe='')}",
        params={"idProperty": "email"},
        allow_not_found=True,
    )
    if existing:
        contact_id = str(existing["id"])
        _request(
            "PATCH",
            f"/crm/v3/objects/contacts/{contact_id}",
            json={"properties": properties},
        )
        return contact_id

    result = _request(
        "POST",
        "/crm/v3/objects/contacts",
        json={"properties": properties},
    )
    return str(result["id"])


def _upsert_company(lead):
    properties = _company_properties(lead)
    if lead.hubspot_company_id:
        result = _request(
            "PATCH",
            f"/crm/v3/objects/companies/{lead.hubspot_company_id}",
            json={"properties": properties},
        )
        return str(result["id"])

    existing = None
    if properties.get("domain"):
        existing = _request(
            "GET",
            f"/crm/v3/objects/companies/{quote(properties['domain'], safe='')}",
            params={"idProperty": "domain"},
            allow_not_found=True,
        )
    if existing is None:
        search = _request(
            "POST",
            "/crm/v3/objects/companies/search",
            json={
                "filterGroups": [{"filters": [{
                    "propertyName": "name",
                    "operator": "EQ",
                    "value": properties["name"],
                }]}],
                "limit": 1,
            },
        )
        results = search.get("results") or []
        existing = results[0] if results else None

    if existing:
        company_id = str(existing["id"])
        _request(
            "PATCH",
            f"/crm/v3/objects/companies/{company_id}",
            json={"properties": properties},
        )
        return company_id

    result = _request(
        "POST",
        "/crm/v3/objects/companies",
        json={"properties": properties},
    )
    return str(result["id"])


def _create_note(contact_id, company_id, note_body):
    result = _request(
        "POST",
        "/crm/v3/objects/notes",
        json={
            "properties": {
                "hs_timestamp": datetime.now(timezone.utc).isoformat(),
                "hs_note_body": note_body[:65536],
            }
        },
    )
    note_id = str(result["id"])
    _request(
        "PUT",
        f"/crm/v4/objects/note/{note_id}/associations/default/contact/{contact_id}",
    )
    _request(
        "PUT",
        f"/crm/v4/objects/note/{note_id}/associations/default/company/{company_id}",
    )


def sync_lead_to_hubspot(lead, *, note_body=None):
    """Upsertne lead až po explicitnom volaní z používateľskej POST akcie."""
    contact_id = _upsert_contact(lead)
    company_id = _upsert_company(lead)
    _request(
        "PUT",
        f"/crm/v4/objects/contact/{contact_id}/associations/default/company/{company_id}",
    )
    if note_body:
        _create_note(contact_id, company_id, note_body)

    lead.hubspot_contact_id = contact_id
    lead.hubspot_company_id = company_id
    lead.hubspot_synced_at = datetime.now(timezone.utc)
    return {
        "contact_id": contact_id,
        "company_id": company_id,
    }
