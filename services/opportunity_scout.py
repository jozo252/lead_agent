from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests
from flask import current_app
from sqlalchemy.exc import IntegrityError

from extensions import db
from models import Campaign, Opportunity, ScoutRun


logger = logging.getLogger(__name__)


BRAVE_WEB_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
TRACKING_QUERY_KEYS = {"fbclid", "gclid", "msclkid"}
OPPORTUNITY_SIGNALS = (
    "dopyt",
    "zákazk",
    "zakazk",
    "hľadáme",
    "hladame",
    "subdodávateľ",
    "subdodavatel",
    "výberové konanie",
    "tender",
    "request for quote",
    "rfq",
    "auftrag",
    "ausschreibung",
    "subunternehmer",
)


def utcnow():
    return datetime.now(timezone.utc)


def normalize_source_url(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = urlparse(value.strip())
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return None
        host = parsed.hostname.casefold()
        if parsed.port:
            host = f"{host}:{parsed.port}"
        query = [
            (key, item)
            for key, item in parse_qsl(parsed.query, keep_blank_values=True)
            if not key.casefold().startswith("utm_")
            and key.casefold() not in TRACKING_QUERY_KEYS
        ]
        return urlunparse(
            (
                parsed.scheme.lower(),
                host,
                parsed.path or "/",
                "",
                urlencode(sorted(query)),
                "",
            )
        )[:1500]
    except (TypeError, ValueError):
        return None


def validate_scout_queries(value):
    if not isinstance(value, list):
        raise ValueError("Lovec zákaziek potrebuje zoznam vyhľadávacích dotazov.")
    queries = []
    for raw_query in value:
        query = " ".join(str(raw_query or "").split())
        if not query:
            continue
        if len(query) > 400 or len(query.split()) > 50:
            raise ValueError("Vyhľadávací dotaz môže mať najviac 400 znakov a 50 slov.")
        if query not in queries:
            queries.append(query)
    if not queries:
        raise ValueError("Lovec zákaziek potrebuje aspoň jeden vyhľadávací dotaz.")
    if len(queries) > 10:
        raise ValueError("Jedna kampaň môže mať najviac 10 vyhľadávacích dotazov.")
    return queries


def brave_web_search(query, *, count=10, country=None, search_lang=None, session=requests):
    api_key = current_app.config.get("BRAVE_API_KEY") or os.environ.get("BRAVE_API_KEY")
    if not api_key:
        raise RuntimeError("Chýba BRAVE_API_KEY.")
    params = {"q": query, "count": max(1, min(int(count), 20))}
    if country:
        params["country"] = str(country).upper()
    if search_lang:
        params["search_lang"] = str(search_lang).lower()
    response = session.get(
        BRAVE_WEB_SEARCH_URL,
        headers={
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": api_key,
        },
        params=params,
        timeout=(5, 15),
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError("Brave Search vrátil neplatný formát odpovede.")
    return data


def _clean_text(value, maximum):
    text = " ".join(str(value or "").split())
    return text[:maximum] or None


def _campaign_keywords(campaign):
    profile = campaign.targeting_profile or {}
    raw_keywords = profile.get("opportunity_keywords") or []
    if not isinstance(raw_keywords, list):
        return []
    return [
        keyword
        for keyword in (_clean_text(item, 100) for item in raw_keywords)
        if keyword
    ][:30]


def score_opportunity(result, campaign):
    title = _clean_text(result.get("title"), 500) or ""
    description = _clean_text(result.get("description"), 4000) or ""
    extra = result.get("extra_snippets") or []
    if not isinstance(extra, list):
        extra = []
    haystack = " ".join([title, description, *map(str, extra)]).casefold()
    keywords = _campaign_keywords(campaign)
    matched_keywords = [keyword for keyword in keywords if keyword.casefold() in haystack]
    matched_signals = [signal for signal in OPPORTUNITY_SIGNALS if signal in haystack]
    score = min(100, 20 + len(matched_keywords) * 15 + min(30, len(matched_signals) * 10))
    reasons = []
    if matched_keywords:
        reasons.append("Kľúčové slová: " + ", ".join(matched_keywords[:5]))
    if matched_signals:
        reasons.append("Signály zákazky: " + ", ".join(matched_signals[:3]))
    if not reasons:
        reasons.append("Kandidát z cieleného vyhľadávacieho dotazu; vyžaduje kontrolu.")
    return score, "; ".join(reasons)


def _web_results(payload):
    web = payload.get("web") if isinstance(payload, dict) else None
    results = web.get("results") if isinstance(web, dict) else None
    return results if isinstance(results, list) else []


def _save_result(campaign, query, result, now):
    if not isinstance(result, dict):
        return "skipped"
    source_url = normalize_source_url(result.get("url"))
    title = _clean_text(result.get("title"), 500)
    if not source_url or not title:
        return "skipped"

    existing = Opportunity.query.filter_by(
        campaign_id=campaign.id,
        source_url=source_url,
    ).first()
    description = _clean_text(result.get("description"), 4000)
    published_text = _clean_text(result.get("page_age") or result.get("age"), 100)
    score, reason = score_opportunity(result, campaign)
    extra = result.get("extra_snippets") or []
    evidence = [
        text
        for text in (_clean_text(item, 500) for item in extra if isinstance(item, str))
        if text
    ][:5]

    if existing is not None:
        existing.last_seen_at = now
        existing.title = title
        existing.description = description
        existing.published_text = published_text
        existing.fit_score = score
        existing.fit_reason = reason
        existing.evidence = evidence or None
        return "refreshed"

    db.session.add(
        Opportunity(
            campaign=campaign,
            business_line=campaign.business_line,
            source_name="brave_web",
            source_url=source_url,
            search_query=query,
            title=title,
            description=description,
            published_text=published_text,
            status="new",
            fit_score=score,
            fit_reason=reason,
            evidence=evidence or None,
            discovered_at=now,
            last_seen_at=now,
        )
    )
    return "discovered"


def _start_run(campaign, queries, now):
    run = ScoutRun(
        campaign=campaign,
        run_date=now.date(),
        status="running",
        query_count=len(queries),
        started_at=now,
    )
    db.session.add(run)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return None
    return run


def run_campaign_scout(campaign, *, search=brave_web_search, now=None):
    if not campaign.scout_enabled:
        return {"campaign_id": campaign.id, "skipped": "Lovec nie je zapnutý."}
    if campaign.status != "active":
        return {"campaign_id": campaign.id, "skipped": "Kampaň nie je aktívna."}

    queries = validate_scout_queries(campaign.scout_queries)
    profile = campaign.targeting_profile or {}
    now = now or utcnow()
    run = _start_run(campaign, queries, now)
    if run is None:
        return {"campaign_id": campaign.id, "skipped": "Dnešný lov už prebehol."}

    discovered = 0
    refreshed = 0
    try:
        for query in queries:
            payload = search(
                query,
                count=min(20, max(5, campaign.batch_size)),
                country=profile.get("search_country"),
                search_lang=profile.get("search_language"),
            )
            seen_in_query = set()
            for result in _web_results(payload):
                normalized_url = normalize_source_url(
                    result.get("url") if isinstance(result, dict) else None
                )
                if not normalized_url or normalized_url in seen_in_query:
                    continue
                seen_in_query.add(normalized_url)
                outcome = _save_result(campaign, query, result, now)
                if outcome == "discovered":
                    discovered += 1
                elif outcome == "refreshed":
                    refreshed += 1

        run.status = "completed"
        run.discovered_count = discovered
        run.refreshed_count = refreshed
        run.finished_at = utcnow()
        campaign.last_scout_run_at = now
        campaign.last_scout_error = None
        db.session.commit()
        return {
            "campaign_id": campaign.id,
            "run_id": run.id,
            "discovered": discovered,
            "refreshed": refreshed,
        }
    except Exception:
        logger.exception(
            "Campaign scout failed",
            extra={"campaign_id": campaign.id, "scout_run_id": run.id},
        )
        db.session.rollback()
        stored_run = db.session.get(ScoutRun, run.id)
        stored_campaign = db.session.get(Campaign, campaign.id)
        stored_run.status = "failed"
        stored_run.error = "Lov sa nepodarilo dokončiť. Podrobnosti sú v serverovom logu."
        stored_run.finished_at = utcnow()
        stored_campaign.last_scout_error = stored_run.error
        db.session.commit()
        return {
            "campaign_id": campaign.id,
            "run_id": run.id,
            "error": stored_run.error,
        }


def run_due_scouts(*, search=brave_web_search, now=None):
    campaigns = Campaign.query.filter_by(status="active", scout_enabled=True).all()
    return [
        run_campaign_scout(campaign, search=search, now=now)
        for campaign in campaigns
    ]
