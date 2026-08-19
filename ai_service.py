import os
from openai import OpenAI
import json


client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))


def generate_lead_message(lead):
    prompt = f"""
Si obchodný asistent pre osobné B2B oslovovanie firiem.

Úloha:
Napíš krátke, normálne a dôveryhodné oslovenie pre potenciálnu spoluprácu.
Nesmie to znieť ako spam, nesmie to byť prehnane predajné a nesmie to sľubovať veci, ktoré nevieme splniť.

Údaje o leade:
Názov firmy: {lead.company_name}
Web: {lead.website or "neuvedené"}
Email: {lead.email or "neuvedené"}
Mesto: {lead.city or "neuvedené"}
Krajina: {lead.country or "neuvedené"}
Segment firmy: {lead.company_segment or "neuvedené"}
Typ ponuky alebo spolupráce: {lead.work_type or "neuvedené"}
Spresnenie ponuky: {lead.work_subtype or "neuvedené"}
Dôvod oslovenia: {lead.reason_to_contact or "neuvedené"}

Pravidlá:
- píš po slovensky,
- tón: slušný, priamy, normálny človek človeku,
- žiadne korporátne frázy,
- maximálne 120 slov,
- používaj iba uvedené fakty a nevymýšľaj schopnosti, referencie ani výsledky,
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
                "content": "Si praktický B2B obchodný asistent. Píšeš stručné a prirodzené správy bez vymyslených tvrdení."
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
Si obchodný analytik databázy firiem.

Úloha:
Vyhodnoť, či dostupné údaje podporujú oslovenie firmy pre uvedený typ ponuky,
spolupráce, produktu alebo dopytu. Nevymýšľaj chýbajúce fakty.

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
- work_type zachovaj z aktuálneho typu práce; ak chýba, použi "Iné"
- nízke skóre použi, ak chýba jasný dôvod oslovenia alebo dôkaz relevancie

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
