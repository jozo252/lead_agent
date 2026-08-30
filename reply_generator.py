import os

from openai import OpenAI


def _client():
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("Chýba OPENAI_API_KEY.")
    return OpenAI(api_key=api_key)


def generate_reply_to_customer(lead, reply):
    prompt = f"""
Si asistent človeka, ktorý oslovuje firmy s konkrétnou B2B ponukou,
návrhom spolupráce, produktom alebo dopytom.

Tvoj cieľ:
- pripraviť krátku, normálnu a slušnú odpoveď
- nepísať ako korporát
- nepôsobiť ako spam
- odpovedať konkrétne na správu firmy
- nevymýšľať schopnosti, referencie ani podmienky
- nesľubovať cenu, termín, dostupnosť ani záväzok
- ak firma chce viac info, ponúkni stručné vysvetlenie a možnosť telefonátu / obhliadky
- ak firma prejavila záujem, smeruj to k ďalšiemu kroku
- ak odpoveď vyžaduje cenu, termín alebo záväzné rozhodnutie, priprav iba potvrdenie prijatia a navrhni osobný kontakt

Informácie o leade:
Firma: {lead.company_name}
Email: {lead.email}
Web: {lead.website}
Typ práce: {lead.work_type}
Podtyp práce: {lead.work_subtype}
Dôvod oslovenia: {lead.reason_to_contact}

Odpoveď od firmy:
Predmet: {reply.subject}
Text:
{reply.text_body}

Napíš odpoveď v slovenčine.
Bez predmetu.
Bez markdownu.
Len čistý text emailu.
"""

    response = _client().chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": "Si praktický B2B asistent. Píšeš krátke, konkrétne a prirodzené e-maily bez záväzkov, ktoré neschválil človek."
            },
            {
                "role": "user",
                "content": prompt
            }
        ],
        temperature=0.4,
    )

    return response.choices[0].message.content.strip()
