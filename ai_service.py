import os
from openai import OpenAI
import json


client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))


def generate_lead_message(lead):
    prompt = f"""
Si obchodný asistent pre malú remeselnú firmu.

Úloha:
Napíš krátke, normálne a dôveryhodné oslovenie pre potenciálnu spoluprácu.
Nesmie to znieť ako spam, nesmie to byť prehnane predajné a nesmie to sľubovať veci, ktoré nevieme splniť.

Kontext o našej firme:
- vieme robiť elektro práce,
- stavebné práce,
- montáže,
- jednoduché zváračské / kovovýrobné práce,
- cieľ je nájsť férovú spoluprácu ako subdodávateľ alebo partner pri zákazkách.

Údaje o leade:
Názov firmy: {lead.company_name}
Web: {lead.website or "neuvedené"}
Email: {lead.email or "neuvedené"}
Mesto: {lead.city or "neuvedené"}
Krajina: {lead.country or "neuvedené"}
Segment firmy: {lead.company_segment or "neuvedené"}
Typ práce, ktorú im chceme ponúknuť: {lead.work_type or "neuvedené"}
Podtyp práce: {lead.work_subtype or "neuvedené"}
Dôvod oslovenia: {lead.reason_to_contact or "neuvedené"}

Pravidlá:
- píš po slovensky,
- tón: slušný, priamy, normálny človek človeku,
- žiadne korporátne frázy,
- maximálne 120 slov,
- nevnucuj sa,
- cieľ je navrhnúť spoluprácu alebo krátky telefonát,
- nepíš predmet emailu, iba telo správy,
- na konci nechaj podpis:
S pozdravom
Adam
"""

    response = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {
                "role": "system",
                "content": "Si praktický obchodný asistent. Píšeš stručné a prirodzené obchodné správy pre remeselníka."
            },
            {
                "role": "user",
                "content": prompt
            }
        ],
        temperature=0.6,
    )

    return response.choices[0].message.content.strip()



def analyze_lead(lead):
    prompt = f"""
Si obchodný analytik pre malú remeselnú firmu.

Naša firma vie robiť:
- elektro práce,
- stavebné práce,
- montáže,
- jednoduché zváračské / kovovýrobné práce.

Úloha:
Vyhodnoť, či má zmysel osloviť túto firmu ako potenciálneho partnera alebo zdroj zákaziek.

Údaje o firme:
Názov firmy: {lead.company_name}
Web: {lead.website or "neuvedené"}
Email: {lead.email or "neuvedené"}
Telefón: {lead.phone or "neuvedené"}
Adresa: {getattr(lead, "address", "") or "neuvedené"}
Mesto: {lead.city or "neuvedené"}
Krajina: {lead.country or "neuvedené"}
Aktuálny segment: {lead.company_segment or "neuvedené"}
Aktuálny typ práce: {lead.work_type or "neuvedené"}
Aktuálny podtyp práce: {lead.work_subtype or "neuvedené"}
Dôvod oslovenia: {lead.reason_to_contact or "neuvedené"}

Pravidlá hodnotenia:
- 1 = slabý lead, strata času
- 2 = možno, ale nízka šanca
- 3 = použiteľný lead
- 4 = dobrý lead
- 5 = veľmi dobrý lead, priorita

Vyber typ práce iba z týchto možností:
- Elektro
- Stavebné práce
- Zváranie / kovovýroba
- Montáže
- Iné

Výstup vráť iba ako čistý JSON bez markdownu:
{{
  "company_segment": "",
  "work_type": "",
  "work_subtype": "",
  "lead_score": 3,
  "reason_to_contact": "",
  "ai_summary": ""
}}
"""

    response = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {
                "role": "system",
                "content": "Si praktický obchodný analytik. Hodnotíš leady realisticky, bez prehnaného optimizmu."
            },
            {
                "role": "user",
                "content": prompt
            }
        ],
        temperature=0.3,
    )

    content = response.choices[0].message.content.strip()

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        raise ValueError(f"AI nevrátila platný JSON: {content}")

    return data