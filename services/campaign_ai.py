import json

from flask import current_app
from openai import OpenAI

from services.campaigns import clean_automated_outreach_body
from services.website_presence import is_website_absence_eligible, website_presence_summary


class CampaignAIError(RuntimeError):
    pass


def _client():
    api_key = current_app.config.get("OPENAI_API_KEY")
    if not api_key:
        raise CampaignAIError("Chýba OPENAI_API_KEY pre AI zacielenie kampane.")
    return OpenAI(api_key=api_key)


def _json_content(response):
    content = response.choices[0].message.content.strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[-1]
        content = content.rsplit("```", 1)[0].strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        raise CampaignAIError(f"AI nevrátila platný JSON: {content[:500]}") from exc


def _string_list(value, maximum=12):
    if not isinstance(value, list):
        return []
    return [str(item).strip()[:120] for item in value if str(item).strip()][:maximum]


def _bounded_int(value, default, minimum, maximum):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def generate_campaign_plan(
    offer_description,
    offer_type,
    offer_stage,
    target_hint="",
    campaign_name="",
):
    stage_instruction = (
        "Produkt je iba validačný koncept. Text musí otvorene hovoriť, že riešenie "
        "pripravujeme alebo overujeme; nesmie tvrdiť, že je hotové ani uvádzať "
        "neexistujúce referencie."
        if offer_stage == "validation"
        else "Ponuka je pripravená. Ani tak nevymýšľaj funkcie, ceny alebo referencie."
    )
    prompt = f"""
Navrhni zacielenie malej slovenskej B2B kampane.

    Názov kampane: {campaign_name or "neuvedený"}
    Typ ponuky: {offer_type}
Ponuka: {offer_description}
Doplňujúca predstava o zákazníkovi: {target_hint or "neuvedená"}

{stage_instruction}

Vráť iba čistý JSON:
{{
  "ideal_customer_profile": "jedna konkrétna veta",
  "nace_keywords": ["2 až 8 výrazov alebo kódov SK NACE"],
  "company_keywords": ["2 až 10 výrazov typických pre vhodnú firmu"],
  "location_keywords": ["0 až 8 názvov obcí použiteľných v registri firiem"],
  "selection_signals": ["overiteľné signály potreby"],
  "exclusion_signals": ["jasné dôvody na vyradenie"],
  "minimum_fit_score": 60,
  "subject_template": "predmet, môže použiť {{company_name}}",
  "body_template": "slovenský e-mail do 100 slov bez oslovenia, názvu firmy, otázky a podpisu; podpis doplní odosielateľský profil"
}}

    Názov kampane a predstavu o zákazníkovi ber ako záväzné zacielenie. Ak názov
    obsahuje segment alebo lokalitu, nesmieš ich nahradiť všeobecným trhom.
    Do location_keywords dávaj názvy obcí, nie názov kraja. Pre Bratislavu použi
    "Bratislava"; ak je uvedený širší región, uveď najviac 8 konkrétnych obcí.
    Text nezačínaj pozdravom „Dobrý deň“, nepoužívaj názov firmy a nepridávaj
    otázku ani výzvu, aby príjemca odpovedal. Odhlasovaciu vetu nepridávaj.
    Výrazy musia byť použiteľné na vyhľadávanie v slovenskej databáze firiem.
"""
    response = _client().chat.completions.create(
        model=current_app.config.get("OPENAI_MODEL", "gpt-4.1-mini"),
        messages=[
            {
                "role": "system",
                "content": (
                    "Si realistický B2B analytik. Vyberáš úzky trh, používaš iba "
                    "overiteľné fakty a nevytváraš klamlivé marketingové tvrdenia."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
    )
    data = _json_content(response)
    profile = {
        "ideal_customer_profile": str(data.get("ideal_customer_profile", "")).strip()[:1000],
        "nace_keywords": _string_list(data.get("nace_keywords"), 8),
        "company_keywords": _string_list(data.get("company_keywords"), 10),
        "location_keywords": _string_list(data.get("location_keywords"), 8),
        "selection_signals": _string_list(data.get("selection_signals"), 10),
        "exclusion_signals": _string_list(data.get("exclusion_signals"), 10),
        "minimum_fit_score": _bounded_int(
            data.get("minimum_fit_score"),
            default=60,
            minimum=0,
            maximum=100,
        ),
        "target_hint": (target_hint or "").strip()[:1000],
    }
    if not profile["nace_keywords"] and not profile["company_keywords"]:
        raise CampaignAIError("AI nevytvorila použiteľné kľúčové slová pre výber firiem.")
    return {
        "targeting_profile": profile,
        "subject_template": str(data.get("subject_template", "")).strip()[:255],
        "body_template": clean_automated_outreach_body(
            data.get("body_template", ""),
            company_name="{company_name}",
        ),
    }


def _website_candidate_data(company):
    summary = website_presence_summary(company)
    return {
        key: summary[key]
        for key in ("status", "label", "eligible", "checked_at", "website_url")
    } | {
        "evidence": [
            {"url": item.get("url"), "title": str(item.get("title") or "")[:300]}
            for item in summary["evidence"][:3] if isinstance(item, dict)
        ],
    }


def rank_campaign_candidates(campaign, candidates):
    if campaign.require_no_website:
        candidates = [company for company in candidates if is_website_absence_eligible(company)]
    if not candidates:
        return []
    candidate_rows = []
    for company in candidates:
        candidate_rows.append(
            {
                "id": company.id,
                "name": company.official_name,
                "municipality": company.municipality,
                "sk_nace_code": company.sk_nace_code,
                "sk_nace_name": company.sk_nace_name,
                "company_type": company.company_type,
                "services": company.services,
                "markets": company.markets,
                "analysis_reason": company.analysis_reason,
                "analysis_evidence": company.analysis_evidence,
                "website_presence": _website_candidate_data(company),
            }
        )

    stage_instruction = (
        "Ide iba o validačný koncept. V každom e-maile transparentne povedz, že "
        "riešenie pripravujeme alebo overujeme."
        if campaign.offer_stage == "validation"
        else "Ponuka je pripravená, ale nevymýšľaj žiadne chýbajúce vlastnosti ani výsledky."
    )
    prompt = f"""
Vyhodnoť firmy pre konkrétnu B2B ponuku a priprav personalizované oslovenia.

Ponuka: {campaign.offer_description}
Ideálny zákazník: {(campaign.targeting_profile or {}).get("ideal_customer_profile", "")}
Požadované SK NACE: {(campaign.targeting_profile or {}).get("nace_keywords", [])}
Firemné výrazy: {(campaign.targeting_profile or {}).get("company_keywords", [])}
Požadované obce: {(campaign.targeting_profile or {}).get("location_keywords", [])}
Signály: {(campaign.targeting_profile or {}).get("selection_signals", [])}
Vylúčenia: {(campaign.targeting_profile or {}).get("exclusion_signals", [])}
Podmienka čerstvého overenia nenájdeného webu: {bool(campaign.require_no_website)}
{stage_instruction}

Firmy:
{json.dumps(candidate_rows, ensure_ascii=False, default=str)}

Vráť iba JSON pole. Pre každú firmu vráť:
{{
  "company_id": 123,
  "fit_score": 0 až 100,
  "fit_reason": "stručný dôvod založený iba na poskytnutých údajoch",
  "subject": "predmet do 255 znakov",
  "body": "prirodzený slovenský e-mail do 100 slov bez oslovenia, názvu firmy, otázky, výzvy na odpoveď a podpisu; podpis doplní odosielateľský profil"
}}

Priamo zodpovedajúce SK NACE a obec potvrdzujú vhodný segment a môžu dostať
60 až 80 bodov aj bez dôkazu konkrétnej potreby. Súvisiaci, ale nie priamy
segment ohodnoť 40 až 59 a nesúvisiaci najviac 39. Chýbajúci dôkaz potreby
nesmieš premeniť na vymyslenú personalizáciu. Nevymýšľaj návštevu webu,
problém, funkcie, cenu, referencie ani výsledky. Nepíš odhlasovaciu vetu.
Stav website_presence=not_found znamená len, že sa web pri obmedzenom
vyhľadávaní nenašiel. Nikdy z toho netvrď, že firma určite nemá web.
Text nezačínaj pozdravom „Dobrý deň“, nepoužívaj v ňom názov firmy a nepíš
žiadnu otázku ani výzvu na odpoveď.
"""
    response = _client().chat.completions.create(
        model=current_app.config.get("OPENAI_MODEL", "gpt-4.1-mini"),
        messages=[
            {
                "role": "system",
                "content": (
                    "Si prísny B2B kvalifikátor a copywriter. Neistotu trestáš "
                    "nízkym skóre a nikdy nevymýšľaš personalizáciu."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
    )
    data = _json_content(response)
    if not isinstance(data, list):
        raise CampaignAIError("AI hodnotenie kandidátov nemá formát poľa.")

    companies_by_id = {company.id: company for company in candidates}
    valid_ids = set(companies_by_id)
    ranked = []
    for item in data:
        try:
            company_id = int(item.get("company_id"))
            fit_score = max(0, min(int(item.get("fit_score", 0)), 100))
        except (AttributeError, TypeError, ValueError):
            continue
        if company_id not in valid_ids:
            continue
        company = companies_by_id[company_id]
        ranked.append(
            {
                "company_id": company_id,
                "fit_score": fit_score,
                "fit_reason": str(item.get("fit_reason", "")).strip()[:1000],
                "subject": str(item.get("subject", "")).strip()[:255],
                "body": clean_automated_outreach_body(
                    item.get("body", ""),
                    company_name=company.official_name,
                ),
            }
        )
    return sorted(ranked, key=lambda item: item["fit_score"], reverse=True)


def generate_landing_page_copy(campaign):
    profile = campaign.targeting_profile or {}
    stage_instruction = (
        "Ide o pripravované riešenie. Jasne ho označ ako pilot alebo koncept a "
        "netvrď, že produkt už existuje."
        if campaign.offer_stage == "validation"
        else "Ponuka je pripravená, ale nevymýšľaj funkcie, výsledky ani referencie."
    )
    prompt = f"""
    Priprav stručný obsah jednej slovenskej B2B landing page.

    Názov kampane: {campaign.name}
    Ponuka: {campaign.offer_description}
    Ideálny zákazník: {profile.get("ideal_customer_profile", "neuvedený")}
    {stage_instruction}

    Vráť iba čistý JSON objekt:
    {{
      "brand_name": "Gallax",
      "eyebrow": "krátke zaradenie ponuky",
      "headline": "jasný výsledok pre zákazníka",
      "subheadline": "jedna konkrétna vysvetľujúca veta",
      "problem_title": "nadpis problému",
      "problem_text": "konkrétny problém bez vymyslených štatistík",
      "solution_title": "Čo presne získate",
      "solution_text": "jasný opis produktu alebo služby",
      "benefits": [
        {{"title": "benefit 1", "text": "vysvetlenie"}},
        {{"title": "benefit 2", "text": "vysvetlenie"}},
        {{"title": "benefit 3", "text": "vysvetlenie"}}
      ],
      "process_title": "Ako to funguje",
      "steps": [
        {{"title": "Krok 1", "text": "vysvetlenie"}},
        {{"title": "Krok 2", "text": "vysvetlenie"}},
        {{"title": "Krok 3", "text": "vysvetlenie"}}
      ],
      "audience_title": "Pre koho je riešenie určené",
      "audience_text": "úzky a konkrétny zákazník",
      "cta_title": "nízkotlaková výzva",
      "cta_text": "čo sa stane po kontakte",
      "cta_label": "text tlačidla",
      "meta_description": "popis do 300 znakov"
    }}

    Text musí vysvetliť, čo zákazník dostane. Nepíš cenu, ak nebola zadaná.
    Nevymýšľaj zákazníkov, logá, referencie, funkcie, úspory ani garancie.
    """
    response = _client().chat.completions.create(
        model=current_app.config.get("OPENAI_MODEL", "gpt-4.1-mini"),
        messages=[
            {
                "role": "system",
                "content": (
                    "Si vecný B2B copywriter. Píšeš dôveryhodne, konkrétne a bez "
                    "marketingových výmyslov."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
    )
    data = _json_content(response)
    if not isinstance(data, dict):
        raise CampaignAIError("AI obsah landing page nemá formát objektu.")
    return data
