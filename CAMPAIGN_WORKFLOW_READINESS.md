# Tri kampane: stav balíka a bezpečné spustenie

## Vývojová kontrola pred nasadením – 6. 9. 2026

Nasledujúce výsledky zaznamenávajú overenie **vo worktree pred nasadením**.
Počas týchto vývojových testov neprebehli reálne e-maily, API vyhľadávanie,
aktivácia kampane ani zmena hlavnej databázy. Stav konkrétneho nasadenia treba
overiť podľa Git revízie, Alembic headu a bežiacej služby. Lokálne MCP môže smerovať
na iný checkout; samotný reštart nie je náhradou za nasadenie a migráciu.

- Tri oddelené profily `WEBS`, `ELEKTRO`, `MG_STAV`, vlastné SMTP/IMAP a podpis.
- Tlačidlo **Odosielatelia → Pripraviť tri neaktívne AI koncepty** pripraví weby,
  elektro a M&G-STAV. Opakované kliknutie ich neduplikuje. Sú to koncepty na
  dopracovanie: bez aktivácie, lovca, follow-upu či vymyslených firemných údajov.
- Filter `require_no_website` rozlišuje neoverený web, nájdený web, nenájdený web
  pri konkrétnej kontrole a chybu. Prázdna databázová hodnota nestačí.
- Jedno výslovne schválené pripomenutie po prvom úspešnom oslovení. Starším
  odoslaným správam sa nevytvára spätne. Text follow-upu tvorí schválená šablóna,
  nie LLM. Podporuje `{company_name}`, `{municipality}`, `{ico}`.
- Čerstvá kontrola inboxu pred pripomenutím; odpoveď, nesúhlas, suppression,
  neoverený kontakt, nový kontakt, zmena kampane či nedostupný inbox posielanie
  zastavia. Aj odpoveď kolegu z inej adresy v rovnakom vlákne ho zastaví.
- Denný limit zahŕňa prvé oslovenia aj follow-upy vrátane neistých SMTP pokusov.
  Zdieľaný databázový zámok a atomická rezervácia chránia pred opakovaním.
- Odpovede a cenové ponuky používajú pôvodnú schránku z histórie. Manuálna cena
  a potvrdenie ponuky zostávajú povinné. Globálne CRM tlačidlá nemôžu obísť
  profilovanú kampaň ani suppression.

## Čo bolo overené

- `python -B -m unittest discover -p 'test_*.py' -q`: **222 testov, OK** (vrátane regresie prechodu zo starej schránky na profil).
- Kompletný UI tok s reálnym kódom zostavenia e-mailu a `MAIL_SUPPRESS_SEND=True`:
  overenie firmy → schválenie → prvé oslovenie → jediný vláknovaný follow-up.
  SMTP/IMAP/externý HTTP boli v integračných testoch blokované/mokované.
- Reply/opt-out, odlišné účty, výpadky IMAP, neúplný inbox, timeout SMTP,
  zlyhanie DB po SMTP, súbehy a zákaz opätovného schválenia neistého odoslania.
- Migrácia `b6d7e8f9a0b1 → c7e9f1a2b3d4 → b6d7e8f9a0b1 → c7e9f1a2b3d4`
  na SQLite backup kópii skutočnej databázy: 177 785 firiem, 19 pôvodných tabuliek.
  Všetky pôvodné počty zachované; hash pôvodných stĺpcov prestavaných tabuliek
  nezmenený; `quick_check=ok`, FK chyby 0, Alembic bez modelového rozdielu.
  Veľkosť a mtime originálu sa nezmenili.
- Skutočný dočasný HTTP server nad kópiou: kampane, detail kampane, nová kampaň,
  odosielatelia, inbox a detail firmy vrátili HTTP 200. Server bol potom zastavený.
- Záložná testovacia kópia zostala v
  `instance/migration-check-20260906T092553Z-7796005d.db` (2 280 607 744 bajtov,
  ignorovaná Gitom). Obsahuje skutočné údaje, preto ju nepublikovať ani nezdieľať.

Reprodukovateľné overenie migrácie (vytvorí **novú kópiu**, zdroj otvorí read-only):

```powershell
python -B scripts/verify_campaign_workflow_migration.py --source "ABSOLUTNA_CESTA_K_ZDROJOVEJ_DB"
```

Skript očakáva pôvodnú revíziu `b6d7e8f9a0b1`. Downgrade odstraňuje iba nové
tabuľky/stĺpce tejto funkcie, a teda aj ich novú históriu; v produkcii ho nerobiť
bez samostatnej zálohy a rozhodnutia o nových údajoch.

## Najkratšia cesta k prvému reálnemu pilotu

1. Pred prenosom tohto balíka do hlavného checkoutu/VPS získať schválenie. Zachovať nesúvisiace
   lokálne úpravy a lokálne `.codex/config.toml`; nekopírovať strojové cesty do
   zdieľanej konfigurácie. Pred migráciou spraviť čerstvú SQLite backup zálohu.
2. Po kontrolovanom nasadení spustiť `python -m flask db upgrade`. Overiť head,
   aplikáciu a existujúce dáta. Scheduler zatiaľ neinštalovať ani nezapínať.
3. Na **Odosielatelia** pripraviť profily/tri koncepty. Vyplniť iba skutočné
   adresy, mená a podpisy. Heslá a hosty nastaviť v privátnej konfigurácii podľa
   `.env.sender-profiles.example`; po zmene prostredia reštartovať príslušný proces.
   Hodnoty konfigurácie sa v UI nezobrazujú, iba chýbajúce položky.
4. V každej kampani dopracovať ponuku, región, zacielenie, prvý text a denný limit.
   Pre prvý pilot odporúčaný limit 3/deň. M&G-STAV, elektro a weby musia mať
   samostatný profil a pravdivé podpisy; cenu ani kvalifikácie nevymýšľať.
5. Webová kampaň: **Overiť weby ďalších 5 kandidátov** spotrebuje najviac 15
   dotazov Brave. Potom vybrať firmy. Jednotlivú firmu (aj staršiu kontrolu)
   možno overiť v jej detaile. Kontrola staršia než 30 dní neplatí. Nejednoznačné
   výsledky sú vyradené, nie považované za firmy bez webu.
6. Najprv na vlastnej testovacej adrese overiť From/Reply-To/podpis a doručenie,
   skutočnú odpoveď cez **Inbox → zvolený profil** a odhlásenie. Vyplnená konfigurácia
   nie je dôkazom správneho hesla, doručiteľnosti, SPF/DKIM/DMARC ani vhodnosti oslovenia.
7. Skontrolovať presný text jedného pripomenutia a podpis. Až potom zapnúť jeho
   checkbox a výslovne schváliť. Zmena nastavení ruší staré čakajúce pripomenutia;
   zmena podpisu odoberie schválenie. Identita schránky s odoslanou históriou je nemenná.
8. Až po schválení vlastného testu a konkrétnej prvej dávky aktivovať kampaň.
   Aktivácia AI kampane schvaľuje aj budúce automaticky pripravované denné dávky;
   nie je to len uloženie konceptu. Scheduler nainštalovať samostatne až po tomto kroku.

## Prevádzkové príkazy

Bezpečný náhľad nevolá SMTP/IMAP/Brave a nemení údaje:

```powershell
python -m flask run-followups
python -m flask run-followups --campaign-id 1 --limit 5
```

**Nasledujúci príkaz naozaj odosiela** – až po krokoch vyššie:

```powershell
python -m flask run-followups --campaign-id 1 --limit 5 --send
```

Na scheduleri má zmysel najprv spracovať schválené follow-upy a až potom existujúci
`run-campaigns`, aby pripomenutia nevyhladovala plná prvá dávka. Prevádzkovať jednu
inštanciu schedulera. Inštalácia balíka ani migrácia scheduler nevytvárajú či nezapínajú.

## Zámerné hranice

- Nie je to úplný autonómny obchodník pre Facebook/Bazoš: lovec používa existujúce
  verejné vyhľadávanie, neprihlasuje sa do skupín, neobchádza obmedzenia a neposiela DM.
- Overovanie webov je explicitná malá dávka, nie automatická platená kontrola celej
  databázy. AI vie zoradiť doložených kandidátov; pravdivosť a povolenie poslať
  garantujú iba kontroly v klasickom kóde a rozhodnutie používateľa.
- Jeden inbox na profil, SMTP/IMAP username rovnaký ako sender_email. Zdieľané relay
  účty/aliasy nie sú podporované. Automatické presúvanie odpovedí mimo Inboxu treba
  pred pilotom vypnúť; presunuté/zmazané správy táto kontrola nevidí.
- Pripravený profil bez vlastnej e-mailovej histórie neblokuje starú globálnu
  synchronizáciu rovnakej schránky. Po prvej profilovanej správe alebo odpovedi ju
  blokuje aj vypnutý profil, aby sa nemiešali identity. Starú neoznačenú históriu
  systém spätne nepriraďuje; jej prechod vyžaduje samostatné overenie pôvodnej schránky.
- Každá kontrola načíta celé relevantné okno, najviac 1000 správ. Prekročenie limitu
  alebo poškodená správa zablokuje odosielanie. Žiadne tiché preskočenie zlyhaní.
- Globálne unikátny Message-ID odpovede zatiaľ ostáva v pôvodnej schéme; tá istá
  správa skopírovaná do dvoch profilov sa zastaví na ručnú kontrolu.
- SMTP nie je transakcia s databázou. `sending`/`unknown` znamená **neopakovať**:
  najprv skontrolovať server/odoslanú poštu podľa Message-ID. Pri follow-upe je ID
  uložené ešte pred SMTP. UI zatiaľ neposkytuje automatické „retry“ ani reconciliáciu.
- Prípadná odpoveď doručená po poslednej kontrole inboxu už rozbehnuté SMTP nemusí
  zastaviť. Doručiteľnosť ani fyzické prijatie správ sa offline testom nepreukazujú.

Po kontrolovanom nasadení nasleduje malý vlastný mailový test, nie pridávanie
ďalších zdrojov alebo masové oslovovanie všetkých troch segmentov naraz.
