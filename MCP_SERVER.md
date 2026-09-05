# Lead Agent MCP

Lokálny STDIO MCP sprístupňuje bezpečnú časť Lead Agenta priamo v Codexe. Číta
príležitosti, leady, cenové požiadavky a metriky. Zapisuje iba dve úzko
ohraničené zmeny: prevod manuálne overenej príležitosti na CRM lead a
pozastavenie aktívnej kampane.

Server zámerne nemá nástroj na odoslanie e-mailu, publikovanie stránky ani
doplnenie či odoslanie ceny. Prevod vytvorí len koncept na kontrolu.

## Dostupné nástroje

| Nástroj | Typ | Výsledok |
| --- | --- | --- |
| `list_new_opportunities` | čítanie | nové príležitosti podľa skóre |
| `convert_verified_opportunity` | zápis s potvrdením | CRM lead a koncept, bez odoslania |
| `list_new_leads` | čítanie | nové a pripravené leady |
| `list_quote_requests` | čítanie | cenové požiadavky; cenu nemení |
| `campaign_metrics` | čítanie | počty podľa kampane a stavov |
| `pause_campaign` | zápis s potvrdením | pozastavená aktívna kampaň |

## Lokálne spustenie

1. Nainštaluj závislosti rovnakým Pythonom, ktorým sa spúšťa aplikácia:

   ```powershell
   python -m pip install -r requirements.txt
   ```

2. Uisti sa, že `DATABASE_URL` ukazuje na správnu databázu, alebo že projektový
   `.env` obsahuje rovnakú hodnotu ako Flask aplikácia. Server sám migrácie
   nespúšťa.

3. Samostatný diagnostický štart:

   ```powershell
   python mcp_server.py
   ```

   STDIO server pri správnom štarte čaká na MCP klienta a nevypisuje bežné
   aplikačné logy na štandardný výstup.

Projektová konfigurácia je v `.codex/config.toml`. Po otvorení dôveryhodného
projektu a reštarte Codex relácie sa server zobrazí medzi MCP servermi. Zápisové
nástroje majú režim schválenia `writes`.

## Overenie

```powershell
python -m unittest test_mcp_server.py
python -m unittest discover
```

Bezpečnostný test overuje aj to, že konverzia nevytvorí žiadny záznam v
`outbound_emails`.
