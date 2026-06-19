from openai import OpenAI

client = OpenAI()


def generate_reply_to_customer(lead, reply):
    prompt = f"""
Si asistent pre elektrikára / remeselníka, ktorý získava zákazky cez firmy.

Tvoj cieľ:
- pripraviť krátku, normálnu a slušnú odpoveď
- nepísať ako korporát
- nepôsobiť ako spam
- odpovedať konkrétne na správu firmy
- nepreceňovať schopnosti
- nenavrhovať cenu, ak sa na ňu nepýtajú
- ak firma chce viac info, ponúkni stručné vysvetlenie a možnosť telefonátu / obhliadky
- ak firma prejavila záujem, smeruj to k ďalšiemu kroku

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

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": "Si praktický asistent pre remeselníka. Píšeš krátke, konkrétne a prirodzené emaily."
            },
            {
                "role": "user",
                "content": prompt
            }
        ],
        temperature=0.4,
    )

    return response.choices[0].message.content.strip()