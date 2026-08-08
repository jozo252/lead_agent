# Kontrakty funkcií v `rpo_sync.py`

Každá položka uvádza: **vstup → spracovanie → výstup**. Funkcie označené
`legacy` sú starší tok, ktorý je ponechaný len kvôli súčasným testovacím
skriptom; produkčný import používa `sync_rpo()` a `upsert_rpo_record()`.

## HTTP a stav synchronizácie

- `utcnow()` — nič → vytvorí aktuálny čas v UTC → `datetime` s časovou zónou.
- `build_http_session()` — nič → nastaví HTTP retry, hlavičky a connection pool → `requests.Session`.
- `get_or_create_sync_state()` — databáza → nájde alebo vytvorí stav `rpo2_organizations` → `SyncState`.
- `datetime_to_iso(value)` — `datetime` → prevedie čas do ISO 8601 v UTC → reťazec pre RPO parameter `since`.
- `parse_datetime(value)` — reťazec dátumu z RPO → bezpečne ho parsuje → UTC `datetime` alebo `None`.
- `ensure_utc(value)` — `datetime` alebo `None` → doplní/prevedie časovú zónu UTC → UTC `datetime` alebo `None`.
- `extract_next_url(response)` — HTTP odpoveď s `Link` hlavičkou → vyberie odkaz `rel=next` → absolútna URL alebo `None`.
- `extract_records(response_data)` — JSON odpoveď RPO → prijme pole alebo známe obaly (`items`, `results`, `data`, `organizations`) → zoznam slovníkov záznamov; inak výnimka.
- `request_page(session, url)` — HTTP session a RPO URL → vykoná GET a preloží chyby HTTP/429 na `RpoSyncError` → úspešná `Response`.
- `fetch_record_detail(session, record)` — stručný RPO záznam s `resource_url` → stiahne jeho detail → slovník detailu alebo `RpoSyncError`.
- `create_initial_sync_url(state, only_ids)` — stav synchronizácie a prepínač detailov → vytvorí sync URL, prípadne s `since` a `only_ids` → URL reťazec.
- `sync_rpo(max_records, only_ids, commit_every, delay_seconds, resume)` — parametre behu + databáza → stránkuje RPO, ukladá záznamy a checkpointy → slovník výsledku (`success`/`partial`) alebo výnimka.

## Parsovanie a normalizácia RPO dát

- `first_non_empty(*values)` — ľubovoľné hodnoty → nájde prvú neprázdnu → hodnota alebo `None`.
- `get_nested(data, *path)` — slovník a cesta kľúčov → bezpečne prejde vnorené polia → hodnota alebo `None`.
- `parse_date(value)` — textový dátum → načíta prvých 10 znakov ISO dátumu → `date` alebo `None`.
- `normalize_ico(value)` — IČO v ľubovoľnom tvare → ponechá číslice a doplní úvodné nuly → normalizované IČO alebo `None`.
- `select_current_record(records)` — zoznam časovo platných položiek RPO → preferuje položku bez `validTo`, potom najnovšie `validFrom` → slovník alebo `None`.
- `extract_codelist_value(value)` — text alebo RPO codelist slovník → rozbalí `value`/vnorené `value` → čistý text alebo `None`.
- `build_street_address(address)` — slovník adresy → spojí `street` a `buildingNumber` → adresa alebo `None`.
- `extract_postal_code(address)` — slovník adresy → vyberie prvé `postalCodes` → PSČ alebo `None`.
- `normalized_rpo_fields(record)` — plný RPO záznam → vyberie aktuálny názov, IČO, adresu a právnu formu → slovník polí modelu `Company`.
- `extract_source_register_name(record)` — RPO záznam → vyberie názov zdrojového registra → text alebo `None`.
- `is_sro_legal_form(legal_form)` — názov právnej formy z RPO → porovná ho s povolenými tvarmi `s.r.o.` → `True`/`False`.
- `extract_address(record)` `legacy` — RPO záznam → vyberie aktuálnu adresu → slovník adresných polí alebo `None`.
- `extract_activities(record)` `legacy` — RPO záznam → preloží aktivity na interné slovníky → zoznam aktivít.

## Filtrovanie a zápis RPO firmy

- `should_skip_record(normalized)` — normalizované polia → kontrola, či existuje IČO alebo názov → `True`/`False`.
- `should_skip_rpo_record(normalized, source_register)` — polia + register → preskočí neidentifikovateľné a ignorované registre → `True`/`False`.
- `has_identity_conflict(company, normalized)` — existujúca firma a nové polia → porovná nenulové IČO → `True`/`False`.
- `choose_company_for_record(existing_source, existing_company_by_ico)` `legacy` — existujúci zdroj/firma → zvolí súvisiacu firmu → `Company` alebo `None`.
- `ensure_company(existing_company, normalized)` `legacy` — existujúca firma + polia → vráti ju alebo vytvorí novú → `Company`.
- `update_company_from_normalized(company, normalized)` `legacy` — firma + normalizované polia → prepíše polia firmy → aktualizovaný `Company`.
- `ensure_company_source(existing_source, company, record)` `legacy` — zdroj, firma a RPO záznam → aktualizuje alebo vytvorí `CompanySource` → `CompanySource`.
- `replace_company_activities(company, record)` — firma + surový RPO záznam → nahradí aktivity priamo zo záznamu → nič.
- `replace_company_activities(company, activities_data)` `legacy, aktívna definícia` — firma + pripravené aktivity → vymaže a znovu vloží aktivity → nič.
- `upsert_company(record)` `legacy` — RPO záznam + databáza → starší upsert firmy, aktivít a zdroja → `Company` alebo `None`.
- `update_company_from_rpo(company, normalized, record)` — firma, polia a RPO záznam → zapíše aktuálne údaje vrátane dátumov aktualizácie → nič.
- `upsert_rpo_record(record)` — RPO záznam + databáza → nájde/vytvorí firmu a `CompanySource`, aktualizuje dáta → `Company` alebo `None`.

## Brave Search a hľadanie kontaktov

Tieto funkcie nepatria do RPO importu; v ďalšom refaktore sa presunú do
`services/company_contacts.py`.

- `normalize_contact_value(contact_type, value)` — typ a kontakt → normalizuje text na malé písmená → kontakt alebo `None`.
- `strip_legal_suffix(name)` — názov firmy → odstráni bežnú právnu formu → názov bez prípony.
- `build_company_search_queries(company)` — `Company`/objekt s názvom a IČO → vytvorí Brave dotazy → zoznam textových dotazov.
- `normalize_search_results(raw_data)` — surový JSON Brave → vyberie a zjednotí výsledky a lokálne kontakty → zoznam výsledkov.
- `fetch_search_results(query)` — Brave dotaz a `BRAVE_API_KEY` → zavolá Brave API → JSON slovník alebo prázdny slovník.
- `extract_domain(url)` — URL alebo doména → odstráni schému, `www` a cestu → doména alebo `None`.
- `is_blocked_domain(url)` — URL → porovná doménu so sociálnymi a nefiremnými doménami → `True`/`False`.
- `is_social_domain(url)` — URL → overí, či doména patrí sociálnej sieti → `True`/`False`.
- `is_search_excluded_domain(url)` — URL → určí, či výsledok netreba vôbec analyzovať → `True`/`False`.
- `is_directory_domain(url)` — URL → rozpozná firemný katalóg → `True`/`False`.
- `filter_search_results(results)` — Brave výsledky → odstráni blokované domény → filtrovaný zoznam.
- `search_company_web(query)` — Brave dotaz → stiahne, normalizuje a filtruje výsledky → zoznam výsledkov.
- `deduplicate_search_results(results)` — výsledky → nechá len jeden výsledok na doménu → zoznam výsledkov.
- `normalize_for_domain(value)` — text → odstráni diakritiku a nealfanumerické znaky → porovnávací reťazec.
- `is_value_in_text(text, value)` — text a hľadaná hodnota → porovná normalizované tvary → `True`/`False`.
- `score_search_result(company, result)` — firma a Brave výsledok → boduje zhodu názvu, domény a obce → celé číslo confidence.
- `score_contact_candidate(company, candidate, phone_sources)` — firma, jeden zdroj a výskyt telefónov → použije váhy IČO `+50`, názov `+25`, obec/adresa `+15`, e-mailová doména `+15`, opakovaný telefón `+10` a príslušné záporné body → confidence `0–100` a rozpis dôkazov.
- `build_derived_website_url(company)` — firma s obchodným názvom → vytvorí opatrný kandidát `.sk` domény → URL alebo `None`.
- `extract_visible_page_text(html_content)` — HTML obsah → odstráni značky, skripty a štýly → čistý text stránky.
- `fetch_derived_website_result(company)` — firma → načíta odvodenú `.sk` doménu a potvrdí názvom/IČO v obsahu → syntetický výsledok alebo `None`.
- `has_trusted_website_result(company, results)` — firma a Brave výsledky → zistí, či už existuje dôveryhodný ne-katalógový web → `True`/`False`.
- `choose_best_website(company, results, minimum_score)` — firma a výsledky → nájde najvyššie skórovaný web nad prahom → slovník webu alebo `None`.
- `find_company_website(company)` `duplicitná definícia` — firma → vyhľadá a deduplikuje Brave výsledky → v súčasnosti slovník najlepšieho webu; staršia definícia vracia zoznam.
- `debug_search_results(response)` — Brave JSON → formátovane vypíše výsledky → nič.
- `extract_contacts_from_search_result(result)` — jeden Brave výsledok → extrahuje weby, e-maily, telefóny a IČO → slovník zoznamov kontaktov.
- `extract_emails(text)` — text → regexom vyberie jedinečné e-maily → zoznam e-mailov.
- `extract_websites(text)` — text → regexom vyberie URL → zoznam webov.
- `normalize_phone(phone)` — telefón → normalizuje slovenské tvary na E.164 → telefón alebo `None`.
- `extract_phones(text)` — text → regexom vyberie slovenské telefóny → zoznam normalizovaných telefónov.
- `extract_icos(text)` — text → vyberie IČO za označením `IČO` → zoznam IČO.
- `choose_verified_website(company, results)` — firma a výsledky → nájde web s IČO zhodou → slovník webu alebo `None`.
- `aggregate_company_contacts(company, results)` — firma a výsledky → vyhodnotí dôveryhodné kontakty aj sociálne kandidáty → slovník domén, kontaktov, `possible_contacts` a dôkazov.
- `select_best_company_contacts(aggregated)` — agregované kontakty → vyberie najlepší web/e-mail/telefón nad prahom → slovník podľa typu.
- `save_best_company_contacts(company, aggregated)` — firma + agregované kontakty + databáza → vytvorí/aktualizuje primárne kontakty bez commitu → zoznam `CompanyContact`.
