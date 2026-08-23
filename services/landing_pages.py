import re
import unicodedata
from urllib.parse import urlparse

from flask import current_app

from models import LandingPage
from services.campaigns import OPT_OUT_FOOTER, VALIDATION_NOTICE


OPERATOR_CONFIG_FIELDS = {
    "name": "LANDING_OPERATOR_NAME",
    "address": "LANDING_OPERATOR_ADDRESS",
    "ico": "LANDING_OPERATOR_ICO",
    "phone": "LANDING_OPERATOR_PHONE",
    "register": "LANDING_OPERATOR_REGISTER",
}


def _clean_text(value, maximum):
    return str(value or "").strip()[:maximum]


def slugify(value):
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    ascii_value = "".join(
        character
        for character in normalized
        if not unicodedata.combining(character)
    )
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_value.casefold()).strip("-")
    return slug[:100].rstrip("-") or "ponuka"


def available_slug(value, landing_page_id=None):
    base = slugify(value)
    candidate = base
    suffix = 2
    while True:
        query = LandingPage.query.filter_by(slug=candidate)
        if landing_page_id is not None:
            query = query.filter(LandingPage.id != landing_page_id)
        if query.first() is None:
            return candidate
        candidate = f"{base[: max(1, 100 - len(str(suffix)) - 1)]}-{suffix}"
        suffix += 1


def safe_image_url(value):
    normalized = _clean_text(value, 1000)
    if not normalized:
        return ""
    parsed = urlparse(normalized)
    if (
        parsed.scheme == "https"
        and parsed.netloc
        and parsed.username is None
        and parsed.password is None
    ):
        return normalized
    return ""


def valid_contact_email(value):
    normalized = _clean_text(value, 255).casefold()
    if not normalized or any(character.isspace() for character in normalized):
        return ""
    if normalized.count("@") != 1:
        return ""
    local, domain = normalized.split("@", 1)
    if not local or "." not in domain or domain.startswith(".") or domain.endswith("."):
        return ""
    return normalized


def default_landing_page_content(campaign):
    profile = campaign.targeting_profile or {}
    audience = _clean_text(profile.get("ideal_customer_profile"), 800)
    audience = audience or "Firmy, ktorým táto ponuka rieši konkrétny prevádzkový problém."
    stage_step = (
        "Spoločne overíme pilot a získame spätnú väzbu."
        if campaign.offer_stage == "validation"
        else "Riešenie nastavíme podľa dohodnutého rozsahu."
    )
    offer = _clean_text(campaign.offer_description, 2000)
    return {
        "brand_name": "Gallax",
        "eyebrow": "Praktické B2B riešenie",
        "headline": _clean_text(campaign.name, 200),
        "subheadline": offer,
        "problem_title": "Prečo toto riešenie vzniká",
        "problem_text": offer,
        "solution_title": "Čo presne získate",
        "solution_text": (
            "Jednoduché riešenie zamerané na jeden konkrétny výsledok, bez "
            "zbytočne komplikovaného zavádzania."
        ),
        "benefits": [
            {"title": "Konkrétny výsledok", "text": offer},
            {"title": "Určené pre správne firmy", "text": audience},
            {
                "title": "Jednoduchý začiatok",
                "text": "Najprv si potvrdíme potrebu a až potom dohodneme ďalší krok.",
            },
        ],
        "process_title": "Ako spolupráca prebieha",
        "steps": [
            {"title": "Krátky rozhovor", "text": "Overíme súčasný stav a očakávaný výsledok."},
            {"title": "Nastavenie", "text": stage_step},
            {"title": "Vyhodnotenie", "text": "Zhodnotíme prínos a dohodneme ďalší postup."},
        ],
        "audience_title": "Pre koho je riešenie určené",
        "audience_text": audience,
        "cta_title": "Zistime, či to dáva zmysel aj pre vás",
        "cta_text": "Stačí krátka odpoveď alebo nezáväzný rozhovor o vašej situácii.",
        "cta_label": "Mám záujem o krátky rozhovor",
        "meta_description": _clean_text(offer, 300),
    }


def _clean_items(value, fallback, maximum=3):
    if not isinstance(value, list):
        value = []
    cleaned = []
    for item in value[:maximum]:
        if not isinstance(item, dict):
            continue
        title = _clean_text(item.get("title"), 120)
        text = _clean_text(item.get("text"), 600)
        if title and text:
            cleaned.append({"title": title, "text": text})
    return cleaned or fallback


def normalize_landing_page_content(value, campaign):
    fallback = default_landing_page_content(campaign)
    source = value if isinstance(value, dict) else {}
    content = {}
    limits = {
        "brand_name": 120,
        "eyebrow": 120,
        "headline": 200,
        "subheadline": 1200,
        "problem_title": 200,
        "problem_text": 2000,
        "solution_title": 200,
        "solution_text": 2000,
        "process_title": 200,
        "audience_title": 200,
        "audience_text": 1600,
        "cta_title": 200,
        "cta_text": 1000,
        "cta_label": 100,
        "meta_description": 300,
    }
    for field, maximum in limits.items():
        content[field] = _clean_text(source.get(field), maximum) or fallback[field]
    content["benefits"] = _clean_items(
        source.get("benefits"),
        fallback["benefits"],
    )
    content["steps"] = _clean_items(
        source.get("steps"),
        fallback["steps"],
    )
    return content


def validation_notice_for(campaign):
    return VALIDATION_NOTICE if campaign.offer_stage == "validation" else ""


def configured_operator_identity():
    return {
        field: _clean_text(current_app.config.get(config_name), 500)
        for field, config_name in OPERATOR_CONFIG_FIELDS.items()
    }


def missing_operator_config():
    identity = configured_operator_identity()
    return [
        config_name
        for field, config_name in OPERATOR_CONFIG_FIELDS.items()
        if not identity[field]
    ]


def configured_landing_page_url(landing_page):
    base_url = str(current_app.config.get("PUBLIC_BASE_URL") or "").rstrip("/")
    parsed = urlparse(base_url)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
    ):
        return ""
    return f"{base_url}/ponuka/{landing_page.slug}"


def public_landing_page_url(campaign):
    landing_page = campaign.landing_page
    if landing_page is None or landing_page.status != "published":
        return ""
    return configured_landing_page_url(landing_page)


def ensure_landing_page_link(body, campaign):
    normalized_body = str(body or "").strip()
    page_url = public_landing_page_url(campaign)
    if not page_url or page_url.casefold() in normalized_body.casefold():
        return normalized_body
    footer = ""
    if normalized_body.casefold().endswith(OPT_OUT_FOOTER.casefold()):
        normalized_body = normalized_body[: -len(OPT_OUT_FOOTER)].rstrip()
        footer = f"\n\n{OPT_OUT_FOOTER}"
    return (
        f"{normalized_body}\n\nViac informácií o ponuke: {page_url}{footer}"
    ).strip()
