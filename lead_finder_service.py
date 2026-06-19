import os
import requests


GOOGLE_PLACES_API_KEY = os.environ.get("GOOGLE_PLACES_API_KEY")


def search_places_text(query, max_results=10):
    """
    Vyhľadá firmy cez Google Places Text Search (New).
    Vracia zoznam normalizovaných výsledkov.
    """

    if not GOOGLE_PLACES_API_KEY:
        raise ValueError("Chýba GOOGLE_PLACES_API_KEY v .env súbore.")

    url = "https://places.googleapis.com/v1/places:searchText"

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": GOOGLE_PLACES_API_KEY,
        "X-Goog-FieldMask": (
            "places.id,"
            "places.displayName,"
            "places.formattedAddress,"
            "places.nationalPhoneNumber,"
            "places.internationalPhoneNumber,"
            "places.websiteUri,"
            "places.businessStatus,"
            "places.primaryType,"
            "places.types"
        ),
    }

    payload = {
        "textQuery": query,
        "languageCode": "sk",
        "regionCode": "SK",
        "maxResultCount": max_results,
    }

    response = requests.post(url, headers=headers, json=payload, timeout=20)
    response.raise_for_status()

    data = response.json()
    places = data.get("places", [])

    results = []

    for place in places:
        display_name = place.get("displayName", {}).get("text", "")

        phone = (
            place.get("nationalPhoneNumber")
            or place.get("internationalPhoneNumber")
            or ""
        )

        results.append({
            "google_place_id": place.get("id", ""),
            "company_name": display_name,
            "website": place.get("websiteUri", ""),
            "phone": phone,
            "address": place.get("formattedAddress", ""),
            "business_status": place.get("businessStatus", ""),
            "primary_type": place.get("primaryType", ""),
            "types": ", ".join(place.get("types", [])),
        })

    return results


def build_search_queries(locations, company_types):
    queries = []

    for location in locations:
        location = location.strip()
        if not location:
            continue

        for company_type in company_types:
            company_type = company_type.strip()
            if not company_type:
                continue

            queries.append(f"{company_type} {location}")

    return queries