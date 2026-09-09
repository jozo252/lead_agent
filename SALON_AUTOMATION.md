# Pravidelná kampaň na salóny

Worker používa existujúce Company, CompanyContact, CampaignRecipient a CampaignFollowUp.
Verejné zdroje hľadá cez Brave. Import vyžaduje jednu zhodnú prevádzku, lokalitu,
viditeľný firemný e-mail a konkrétne telefonické/e-mailové objednávanie. Nález na
jednej stránke nedokazuje absenciu rezervačného systému všade; e-mail to netvrdí.
Nečitateľné stránky (vrátane Notino HTTP 403), nejasné kontakty a online kalendáre
sa preskočia. Názov prevádzky sa neprezentuje ako overené obchodné meno ani IČO.

## Nastavenie pilotu

- Existujúca kampaň 6, Poprad, Svit, Kežmarok.
- Najviac 15 prvých oslovení vrátane 6 historických, potom vyhodnotiť odpovede.
- Denný spoločný limit 3 e-maily, najviac 3 nové kontakty v dávke.
- Jeden follow-up 7 kalendárnych dní po úspešnom odoslaní; voľné denné miesto majú
  prednostne pripomenutia. Žiadne automatické ďalšie pripomenutie.
- Po–Pi o 08:30 a 15:00 Europe/Bratislava. Bez doháňania po výpadku.
- Vyhľadávanie najviac raz za UTC deň, najviac 12 načítaných stránok.
- Limit 90 dní na nové oslovenie; už uložené e-maily/prevádzky sa neduplikujú.
- Presný text, zdroj kontaktu, informačná stránka a možnosť odmietnuť správy sú
  doplnené klasickým kódom. LLM nemá právo meniť obsah ani rozhodnúť o odoslaní.

## Režimy

`python -m flask --app wsgi run-salon-cycle --campaign-id 6`
je iba náhľad bez siete a zápisu.

`python -m flask --app wsgi discover-salons --campaign-id 6`
robí živé vyhľadávanie bez zápisu. `--save` ukladá kontakty, nikdy neposiela.

`python -m flask --app wsgi run-salon-cycle --campaign-id 6 --collect-only`
skontroluje celú relevantnú schránku a uloží nové kontakty bez odosielania.

`python -m flask --app wsgi run-salon-cycle --campaign-id 6 --send`
vyžaduje aktívnu kampaň s automation_enabled a pripraveným profilom. Najprv
synchronizuje inbox, odošle splatné schválené follow-upy, vyhľadá nové kontakty,
znovu načíta inbox a spustí dennú dávku. Pri follow-upe znovu overuje verejný zdroj.
Odpoveď, odhlásenie a nejasný SMTP výsledok zabraňujú opakovaniu.

## Nasadenie

Použiť `deploy/lead-agent-salons@.service` a `.timer`, inštanciu `@6`.
Šablóna je zámerne v režime `--collect-only`. Pre odosielanie treba schválené
nastavenia kampane, zapnúť jej automatizáciu/follow-up a v systemd drop-in zmeniť
`SALON_WORKER_MODE` na `--send`. Zapnutie odosielania nemení historické správy.

Pred zmenou produkčných dát vytvoriť konzistentnú SQLite zálohu, `quick_check`,
prehrať `scripts/configure_salon_pilot.py --database KOPIA --apply`, porovnať
chránenú históriu a až potom použiť produkčnú databázu. Tento skript vždy nastaví
iba zber kontaktov a neposiela. Odosielanie ostáva vypnuté do potvrdenia predmetu
podnikania používateľom; samotná informačná stránka túto skutočnosť nepotvrdzuje.

## Overenie

`test_salon_discovery`, `test_salon_cycle`, `test_campaign_privacy` a
`test_campaign_inbox_command` pokrývajú nové správanie. Spustiť aj existujúce testy
kampaní, limitov, odpovedí, profilov, zámkov a zabezpečenia. Použiť izolované SQLite
databázy, zablokovanú sieť a `PYTHON_DOTENV_DISABLED=1`.

`scripts/salon_mail_selftest.py --send --receipt NOVY_SUBOR` odošle presne dva
interné testy medzi vlastnými schránkami adam@gallax.io a elektro@gallax.io,
overí doručenie a vlákno. Používa iba databázu v pamäti. Existujúci súbor záznamu
zabráni automatickému opakovaniu neistého testu. Bežné testy tento skript nespúšťajú.
