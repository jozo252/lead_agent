# Ďalší postup projektu

## Cieľ prvej verzie

Zistiť, či databáza stavebných a elektro firiem prinesie reálne dopyty na
subdodávateľské kapacity. Databáza je interný nástroj pre prospecting a CRM,
nie samostatný produkt.

## Dáta pred oslovením

### RPO

- IČO, názov, právna forma, stav a adresa
- SK NACE: kód a názov hlavnej činnosti
- predmety činnosti

### Web a AI enrichment

Polia ostávajú prázdne, kým nemajú dôveryhodný zdroj:

- `employee_count`, `employee_count_source`
- `company_type`, `services`, `markets`
- `works_abroad`, `regions`
- `subcontractor_need`, `outreach_relevant`
- `analysis_reason`, `analysis_evidence`, `website_analyzed_at`

Web je zdroj pravdy. AI má z relevantných stránok (domov, služby, realizácie,
o nás, kariéra a kontakt) iba vytvoriť štruktúrované hodnoty a dôkazy.

## Postup experimentu

1. Vyfiltrovať prvých 100 až 300 elektro/stavebných firiem podľa SK NACE a regiónu.
2. Doplniť kontakty a ručne overiť kandidátne kontakty.
3. Pre firmy s funkčným webom vytiahnuť relevantné stránky a uložiť webový text.
4. Nechať AI klasifikovať segment, služby, regióny a signály potreby kapacity.
5. Vybrať top 30 firiem, osloviť ich a merať odpovede.

## Dáta po oslovení

Tieto dáta vznikajú až v CRM komunikácii a majú vyššiu obchodnú hodnotu než
ďalšie verejné registre:

- stav potreby kapacity
- typ práce, počet pracovníkov, lokalita a termín
- požiadavka na cenu
- rozhodovateľ
- follow-up dátum a výsledok (zákazka, neskôr, nezáujem)

Po prvých 100 osloveniach vyhodnotiť, ktoré predbežné signály naozaj vedú k
odpovediam a zákazkám. Až potom škálovať enrichment alebo riešiť predaj softvéru.

## Pokrok — 9. august 2026

- RPO import je pripravený na veľké dávky: `sync-rpo --max-records 1000` ukladá
  checkpoint po celej stránke a ďalší beh bezpečne pokračuje cez `next_url`.
- Sync výstup a databáza sledujú načítané, uložené a preskočené RPO záznamy.
- Databáza má približne 7 200 importovaných firiem `s.r.o.`.
- Kontaktný enrichment ukladá `contacts_checked_at`; firmy bez nájdeného kontaktu
  sa pri bežnej dávke znovu nespúšťajú.
- Prehľad firiem má filtre podľa SK NACE, obce/regiónu, kontaktov, webu,
  analýzy a obchodného profilu.
- Z prehľadu sa dajú cez POST spustiť filtrované dávky hľadania kontaktov
  (1–100 firiem) a AI webovej analýzy (1–25 firiem).
- Radius okolo obce je odložený; vyžaduje samostatné geokódovanie a cache
  súradníc obcí.

## Ciele — 10. august 2026

1. Pokračovať v RPO importe po dávkach 1 000 až aspoň na 10 000 firiem.
2. Vybrať prvý konkrétny segment cez SK NACE a región (napr. elektro alebo stavby).
3. Spustiť kontaktný enrichment iba pre tento segment po dávkach 25–50 firiem.
4. Ručne skontrolovať 20 výsledkov a zapísať reálnu úspešnosť kontaktov.
5. Pre 5–10 firiem s uloženým webom spustiť AI analýzu a vyhodnotiť obchodnú relevanciu.

## Pokrok — 10. august 2026

- Zoznam firiem má stránkovanie po 100 záznamoch; filtre sa zachovávajú pri
  prepínaní strán a batch akcie pracujú nad celým vyfiltrovaným segmentom.
- Hľadanie kontaktov sa dá spustiť z webu iba pre zvolený SK NACE/región a iba
  pre ešte neskontrolované firmy bez kontaktu.
- Kontaktné pravidlá lepšie rozpoznávajú domény odvodené z názvu firmy vrátane
  `spol. s r.o.`, variantov s `Slovakia` a domén potvrdených IČO-overeným e-mailom.
- Firemné katalógy (vrátane `infoma.sk` a `slovakregion.sk`) a cudzie country
  domény ako `.pt`, `.de` alebo `.cz` sa nevyberajú ako primárny web.
- Opravené a znovu spracované vzorové firmy ANTES GM, AJAP, DATS Slovakia a DIAFAN.
- Posledná dávka kontaktov našla kontakt pri 16 z 25 firiem (64 %).
- Z detailu firmy sa dá priamo odoslať e-mail: automaticky sa vytvorí/prepojí
  CRM lead, uloží aktivita a nastaví follow-up.

## Ciele — 11. august 2026

1. Skontrolovať SMTP nastavenie a poslať testovací e-mail najprv na vlastnú adresu.
2. Vybrať 10 firiem z jedného segmentu s primerane kvalitným e-mailom a webom.
3. Manuálne overiť týchto 10 kontaktov a pripraviť krátky personalizovaný text.
4. Osloviť prvých 5–10 firiem cez detail firmy a nastaviť follow-up o 5 dní.
5. Pokračovať v RPO importe po dávkach 1 000 bez spúšťania masového outreachu.
6. Zapísať počet odoslaných e-mailov, odpovedí a problémov s doručiteľnosťou.

## Pokrok — 11. august 2026

- SMTP test prešiel bez zmeny firemných alebo CRM dát.
- Pridaný `test-imap` príkaz na bezpečné overenie prihlásenia do Gmail inboxu.
- Odpovede z IMAP sa už párujú najprv cez `Message-ID`, `In-Reply-To` a
  `References`, nie iba cez odosielateľa.
- Každý odoslaný e-mail sa uloží do `outbound_emails`; prijatá odpoveď sa uloží
  do `EmailReply` a rovnaký e-mail sa nezapíše duplicitne.

## Ciele — 12. august 2026

1. Spustiť `python -m flask test-imap` a potvrdiť prístup do Gmail inboxu.
2. Vybrať 5 kvalitných firiem z jedného obchodného segmentu.
3. Pri každej manuálne overiť web, e-mail a relevanciu pred odoslaním.
4. Napísať a odoslať 3–5 krátkych personalizovaných e-mailov cez detail firmy.
5. Nastaviť follow-up o 5 dní a v CRM skontrolovať uložené odoslané správy.
6. Po prvej odpovedi overiť párovanie cez tlačidlo „Skontrolovať odpoveď“.

## Pokrok — 13. august 2026

- Prebehol audit hlavnej aplikácie, rout, emailového toku a všetkých používaných
  šablón. Starý Postmark inbound webhook bol odstránený; odpovede sa spracúvajú
  cez Gmail IMAP a ukladajú sa do `EmailReply`.
- Šablóny boli zjednotené so svetlým dizajnom a boli doplnené chýbajúce CSS
  premenné, aby Inbox, detail firmy a detail leadu nepoužívali neexistujúce štýly.
- Pribudol samostatný `/dashboard` s počtom firiem, email kontaktov, leadov,
  oslovených leadov, odpovedí, splatných follow-upov a e-mailovou konverziou z
  `OutboundEmail` a `EmailReply`.
- Dashboard obsahuje dennú pracovnú frontu: splatné follow-upy, leady bez odpovede
  viac ako 5 dní a prijaté e-maily, na ktoré ešte nebola odoslaná reakcia.
- Pridané testy dashboardu a pracovných front. Celá aktuálna sada má 27
  prechádzajúcich testov.

## Ciele — 14. august 2026

1. Otvoriť `/dashboard` nad reálnou databázou a skontrolovať, či pracovné fronty
   ukazujú správne firmy a e-maily.
2. Vybrať 10 kvalitných firiem z jedného segmentu, manuálne overiť ich e-mail a
   pripraviť personalizované oslovenie.
3. Odoslať prvú malú dávku 5 až 10 e-mailov, pri každom nastaviť follow-up o 5 dní.
4. Použiť Inbox a dashboard na kontrolu odpovedí; pri odpovedi otestovať odoslanie
   reakcie v pôvodnom e-mailovom vlákne.
5. Zapisovať dôvod, prečo bol kontakt dobrý alebo zlý, aby sa neskôr dalo doladiť
   confidence skóre a výber kandidátnych kontaktov.

## Pokrok — 18. august 2026

- Pribudli samostatné kampane s typom ponuky, šablónou, denným limitom a
  vlastnou históriou príjemcov; jedna firma môže byť v rôznych kampaniach bez
  prepísania predchádzajúcej ponuky.
- Firmy z aktuálneho filtra sa dajú pridať do kampane ako neschválené návrhy.
  Každý text sa pred odoslaním upraví a manuálne schváli.
- Odosielanie znovu kontroluje suppression zoznam, rešpektuje denný limit a
  neistý stav po páde automaticky neopakuje.
- Suppression podporuje e-mail, doménu aj IČO; nedoručenie a odmietnutie ďalších
  správ sa dajú zapísať priamo ako výsledok kampane.
- Databáza firiem má filtre tržieb, stavu oslovenia, kvality e-mailu a radiusu.
  Radius používa 5 233 poštových lokalít z GeoNames (CC BY 4.0), nie platené
  geokódovanie jednotlivých adries.

## Najbližší obchodný test

1. Vytvoriť jednu konkrétnu ponuku a nastaviť denný limit najviac 10–20 správ.
2. Vyfiltrovať 20–30 firiem podľa SK NACE, radiusu a dostupného e-mailu.
3. Pri manuálnej kampani overiť kontakt a schváliť každý návrh osobitne;
   pri automatickej kampani schváliť celý experiment až po kontrole zacielenia
   a základného textu.
4. Merať doručenie, odpoveď, pozitívny záujem a dohodnutý ďalší krok.
5. Chatové ovládanie pridať až po overení tohto deterministického workflow.

## Automatické produktové kampane

Automatická kampaň je riadený experiment, nie neobmedzený agent:

1. Zadaj produkt, štádium ponuky (hotová alebo validačný test), voliteľný opis
   zákazníka, celkový cieľ a dennú dávku.
2. AI pripraví úzky profil zákazníka, vyhľadávacie výrazy a základný e-mail.
3. Pred aktiváciou skontroluj a podľa potreby uprav zacielenie aj text.
4. Tlačidlo `Vybrať firmy zo SK NACE` použije pripravené SK NACE a firemné
   kľúčové slová, AI firmy zoradí a uloží ich ako koncepty bez odoslania.
5. Aktivácia kampane schváli automaticky vybrané koncepty aj budúce denné dávky.
6. Každý denný beh vyberie ďalších kandidátov z databázy, AI ich zoradí, pre vybrané
   firmy doplní kontakt, odošle najviac povolenú dávku a vytvorí CRM lead.
7. Stav `paused` alebo `completed` ďalšie spracovanie okamžite zastaví.

Jednu dávku možno spustiť tlačidlom v detaile kampane. Pre pravidelný beh má
operačný scheduler spustiť raz denne:

```powershell
python -m flask run-campaigns
```

Príkaz je idempotentný v rámci UTC dňa a vždy znovu kontroluje suppression,
cooldown, denný limit a celkový cieľ. Prepínač `--force` je iba na vedomé
opakovanie dávky; limity zostávajú aktívne. Scheduler ani živá kampaň sa pri
lokálnej implementácii automaticky nezapínajú.

## Lovec zákaziek

Každá kampaň môže patriť do odboru stavebníctvo, elektro alebo softvér a mať
vlastný zoznam vyhľadávacích dotazov. Lovec používa Brave Search, ukladá názov,
URL, opis, dôkazy a jednoduché skóre zhody. Rovnakú URL v rámci kampane uloží iba
raz a ďalší nález ju obnoví.

Lovec je predvolene vypnutý. Nastav ho v detaile kampane, doplň `BRAVE_API_KEY`
do lokálneho `.env`, kampaň aktivuj a spusti jeden denný beh tlačidlom alebo:

```powershell
python -m flask run-scouts
```

Konkrétnu kampaň možno spustiť cez `python -m flask run-scouts --campaign-id ID`.
Každá kampaň sa vykoná najviac raz za UTC deň, aj keď vyhľadávanie zlyhá. Lovec
iba číta verejné výsledky a zapisuje príležitosti. Nevytvára príjemcov, neposiela
správy a nevytvára cenu. Ďalší krok je ručné overenie zdroja a rozhodnutie, či sa
má príležitosť zmeniť na lead.

V detaile kampane sa dá nález rozbaliť a po manuálnom overení zmeniť na CRM
lead. Povinné je potvrdenie aktuálnosti, zdroj kontaktu, poznámka k overeniu a
aspoň jeden overený e-mail alebo telefón. Lead dostane iba editovateľný koncept
z existujúcej šablóny kampane; táto akcia nič neposiela a nedopĺňa cenu ani
identitu odosielateľa.

## Cenová brána

Keď prijatá odpoveď vyžaduje cenu, otvor detail leadu a pri konkrétnej odpovedi
klikni `Zákazník žiada cenu`. Vznikne iba jedna požiadavka v stave
`awaiting_price`. V časti `Cenové požiadavky` doplň cenu, menu, jednotku, údaj
o DPH, rozsah, platnosť a potvrdený podpis odosielateľa.

Tlačidlo `Uložiť cenu a odoslať` je zároveň výslovným oprávnením pre túto jednu
ponuku. Požiadavka sa pred SMTP volaním atomicky presunie do stavu `sending`,
preto ju dva súbežné procesy neodošlú dvakrát. Ak výsledok SMTP alebo následného
zápisu nemožno potvrdiť, stav je `unknown` a pred ďalšou akciou treba skontrolovať
odoslanú poštu. Cena ani podpis sa nedopočítavajú cez AI.

Rozpoznanie požiadavky na cenu je zatiaľ manuálne. Automatickú klasifikáciu
odpovedí možno pridať až nad týmto deterministickým stavovým tokom; nejasná
klasifikácia musí zostať na manuálnu kontrolu.

## Lokálne MCP ovládanie

Projekt obsahuje lokálny STDIO server `mcp_server.py` a projektovú konfiguráciu
`.codex/config.toml`. Codex cezň môže čítať príležitosti, leady, cenové
požiadavky a metriky, po potvrdení previesť overenú príležitosť na koncept leadu
alebo pozastaviť aktívnu kampaň. MCP neposiela e-maily, neurčuje cenu a
nepublikuje landing page. Spustenie a testovanie opisuje `MCP_SERVER.md`.

## Landing page ku kampani

Každá kampaň môže mať jednu jednoduchú verejnú landing page:

1. V detaile kampane klikni na `Vytvoriť landing page`.
2. AI pripraví štruktúrovaný koncept; ak AI nie je dostupná, vytvorí sa
   upraviteľný základ z údajov kampane.
3. Uprav nadpis, problém, opis riešenia, tri prínosy, tri kroky, cieľovú
   skupinu, kontakt a voliteľnú HTTPS adresu obrázka.
4. Skontroluj tajný náhľad a až potom stránku publikuj.
5. Verejná adresa má tvar `/ponuka/<slug>`. Koncept na nej vracia 404.

Na VPS nastav v `.env` verejnú HTTPS adresu aplikácie, napríklad:

```dotenv
PUBLIC_BASE_URL=https://lead.gallax.io
```

Ak je landing page publikovaná a `PUBLIC_BASE_URL` je platná HTTPS adresa,
Lead Agent pridá jej odkaz do novo pripravených kampaňových e-mailov pred
odhlasovaciu vetu. Už existujúce návrhy príjemcov sa spätne neprepisujú.

## Bezpečné produkčné spustenie

Pred prvou externou dávkou:

1. Vytvoriť čerstvú SQLite zálohu cez SQLite backup mechanizmus a overiť, že sa dá otvoriť.
2. Commitnúť a na kópii databázy overiť všetky Alembic migrácie.
3. Na VPS nastaviť `APP_ENV=production` a silný unikátny `SECRET_KEY`.
4. Doplniť `LANDING_OPERATOR_*` hodnoty; bez kompletnej identity sa landing page
   nesmie publikovať.
5. V Nginxe vypnúť Basic Auth iba pre `location ^~ /ponuka/`; všetky CRM routy
   musia zostať chránené. Vzor je v `deploy/nginx-lead-agent.conf.example`.
6. Scheduler `run-campaigns` spúšťať iba v jednej inštancii. Databázový zámok
   kampane je poistka proti súbehu s ručným spustením, nie náhrada za správnu prevádzku.
7. Najprv poslať testy na vlastné adresy, overiť IMAP párovanie, automatický
   opt-out a až potom schváliť malú dávku externých príjemcov.
