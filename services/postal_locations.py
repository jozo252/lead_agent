import io
import math
import unicodedata
import zipfile
from pathlib import PurePosixPath

import requests

from extensions import db
from models import PostalLocation


GEONAMES_SK_POSTAL_URL = "https://download.geonames.org/export/zip/SK.zip"
MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024


class LocationLookupError(ValueError):
    pass


def normalize_postal_code(value):
    return "".join(character for character in (value or "") if character.isdigit())


def normalize_place_name(value):
    normalized = unicodedata.normalize("NFKD", (value or "").strip())
    ascii_name = "".join(
        character for character in normalized if not unicodedata.combining(character)
    )
    return " ".join(ascii_name.casefold().split())


def parse_geonames_postal_text(text):
    """Parse GeoNames tab-separated postal data into normalized dictionaries."""
    rows = []

    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue

        values = line.split("\t")
        if len(values) < 12:
            raise ValueError(
                f"GeoNames riadok {line_number} má iba {len(values)} stĺpcov."
            )

        postal_code = normalize_postal_code(values[1])
        place_name = values[2].strip()
        if not postal_code or not place_name:
            continue

        try:
            latitude = float(values[9])
            longitude = float(values[10])
            accuracy = int(values[11]) if values[11].strip() else None
        except ValueError as exc:
            raise ValueError(
                f"GeoNames riadok {line_number} má neplatné súradnice."
            ) from exc

        rows.append(
            {
                "postal_code": postal_code,
                "place_name": place_name,
                "search_name": normalize_place_name(place_name),
                "admin_name_1": values[3].strip() or None,
                "admin_name_2": values[5].strip() or None,
                "latitude": latitude,
                "longitude": longitude,
                "accuracy": accuracy,
                "source": "geonames",
            }
        )

    return rows


def postal_rows_from_zip(payload):
    if len(payload) > MAX_DOWNLOAD_BYTES:
        raise ValueError("GeoNames archív je neočakávane veľký.")

    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        text_files = [
            name
            for name in archive.namelist()
            if PurePosixPath(name).name.casefold() == "sk.txt"
        ]
        if len(text_files) != 1:
            raise ValueError("GeoNames archív neobsahuje očakávaný súbor SK.txt.")
        text = archive.read(text_files[0]).decode("utf-8")

    return parse_geonames_postal_text(text)


def download_geonames_postal_rows():
    response = requests.get(
        GEONAMES_SK_POSTAL_URL,
        timeout=30,
        allow_redirects=False,
    )
    response.raise_for_status()
    return postal_rows_from_zip(response.content)


def import_postal_locations(rows):
    """Idempotently insert or refresh postal locations and return row counts."""
    existing = {
        (location.postal_code, location.place_name): location
        for location in PostalLocation.query.all()
    }
    inserted = 0
    updated = 0

    for values in rows:
        key = (values["postal_code"], values["place_name"])
        location = existing.get(key)

        if location is None:
            location = PostalLocation(**values)
            db.session.add(location)
            existing[key] = location
            inserted += 1
            continue

        for field, value in values.items():
            setattr(location, field, value)
        updated += 1

    return {"inserted": inserted, "updated": updated, "total": len(rows)}


def haversine_km(latitude_1, longitude_1, latitude_2, longitude_2):
    earth_radius_km = 6371.0088
    lat_1 = math.radians(latitude_1)
    lat_2 = math.radians(latitude_2)
    lat_delta = math.radians(latitude_2 - latitude_1)
    lon_delta = math.radians(longitude_2 - longitude_1)
    value = (
        math.sin(lat_delta / 2) ** 2
        + math.cos(lat_1) * math.cos(lat_2) * math.sin(lon_delta / 2) ** 2
    )
    return earth_radius_km * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))


def nearby_postal_codes(center_value, radius_km):
    """Resolve a place and return normalized postal codes within the radius."""
    if PostalLocation.query.first() is None:
        raise LocationLookupError(
            "Chýbajú poštové lokality. Spusti `flask import-postal-locations`."
        )

    center_parts = [part.strip() for part in (center_value or "").split(",")]
    search_name = normalize_place_name(center_parts[0] if center_parts else "")
    district_name = normalize_place_name(center_parts[1]) if len(center_parts) > 1 else ""

    if not search_name:
        raise LocationLookupError("Zadaj obec, od ktorej sa má počítať radius.")

    center_locations = PostalLocation.query.filter_by(search_name=search_name).all()
    if district_name:
        center_locations = [
            location
            for location in center_locations
            if normalize_place_name(location.admin_name_2) == district_name
        ]

    if not center_locations:
        raise LocationLookupError(
            f"Obec „{center_value}“ sa v poštových lokalitách nenašla."
        )

    districts = {
        normalize_place_name(location.admin_name_2)
        for location in center_locations
        if location.admin_name_2
    }
    if not district_name and len(districts) > 1:
        examples = ", ".join(
            sorted({location.admin_name_2 for location in center_locations if location.admin_name_2})
        )
        raise LocationLookupError(
            f"Obec „{center_value}“ je nejednoznačná. Pridaj okres, napr. „obec, okres“. "
            f"Možnosti: {examples}."
        )

    center_latitude = sum(item.latitude for item in center_locations) / len(center_locations)
    center_longitude = sum(item.longitude for item in center_locations) / len(center_locations)
    latitude_delta = radius_km / 111.0
    longitude_delta = radius_km / max(
        1.0,
        111.0 * math.cos(math.radians(center_latitude)),
    )

    candidates = PostalLocation.query.filter(
        PostalLocation.latitude.between(
            center_latitude - latitude_delta,
            center_latitude + latitude_delta,
        ),
        PostalLocation.longitude.between(
            center_longitude - longitude_delta,
            center_longitude + longitude_delta,
        ),
    ).all()

    return {
        location.postal_code
        for location in candidates
        if haversine_km(
            center_latitude,
            center_longitude,
            location.latitude,
            location.longitude,
        )
        <= radius_km
    }
