# Lead Agent as the workflow backend

## Target MVP

`RPO on VPS -> campaign draft batch -> ChatGPT Work enrichment/scoring -> manual HubSpot sync -> manual approval -> Hostinger SMTP -> Hostinger IMAP -> AI reply draft -> manual approval/send -> manual HubSpot update`

No integration endpoint approves recipients, sends mail, or writes to HubSpot automatically.

## Development update – 2026-09-06

The worktree now contains isolated sender profiles, evidence-backed no-website
selection and an explicitly approved one-shot follow-up worker. A code release alone
does not enable sending or install a scheduler. The freeze on live autonomous scheduling below still
applies until a real own-address pilot is approved. See
`CAMPAIGN_WORKFLOW_READINESS.md` for verification, limits and rollout steps.

## Keep and reuse

- `Company`, `CompanySource`, `SyncState`, `CompanyActivity`: RPO source of truth and resumable sync.
- `CompanyContact` and `services/rpo_sync.py`: contact discovery, validation, confidence and source evidence.
- `Campaign`, `CampaignRecipient`, `Suppression`: batch selection, campaign-specific score, editable drafts, approval and opt-out safety.
- `services/campaign_delivery.py`: daily limits, delivery locking, suppression recheck and outbound history.
- `Lead`, `LeadActivity`, `OutboundEmail`, `EmailReply`: local operational timeline and email threading.
- `email_checker_service.py`, `reply_generator.py`: IMAP reply import and AI draft generation.

## Freeze for the MVP

- Legacy Google Places lead discovery and the standalone `Lead`-first prospecting flow.
- Autonomous `run-campaigns` scheduling. Use manual batches until the complete workflow is verified.
- Landing-page generation and RÚZ financial enrichment; neither is required for the first end-to-end test.
- One-off debug scripts (`testing.py`, `scraper.py`, `list_companies.py`, `db_fast_test.py`). They are not production entry points.

Nothing in this group needs to be deleted before the MVP proves useful.

## Minimal additions

- Work API: `GET /integrations/work/campaigns/<id>/batch` and `POST /integrations/work/campaigns/<id>/results` with `X-Work-Token`.
- Work writes only normalized company analysis and `CampaignRecipient.fit_score` / `fit_reason`; recipient status stays `draft`.
- HubSpot adapter upserts a company and contact and can add a reply note. Calls are reachable only through explicit user-confirmed forms.
- Hostinger-compatible SMTP SSL and configurable IMAP port.
- Three nullable HubSpot sync fields on the existing `Lead`; no duplicate CRM model.

## First end-to-end test

1. Back up the SQLite database and apply `python -m flask db upgrade`.
2. Configure `.env` from `.env.example`; keep campaign automation disabled.
3. Run RPO sync and contact enrichment for one narrow segment.
4. Create one manual campaign and add 5 companies as draft recipients.
5. Export the Work batch, return scores and enrichment, and confirm all recipients remain drafts.
6. Review one company and click **Synchronizovať do HubSpotu**.
7. Approve one email and send it first to an address controlled by the operator.
8. Sync IMAP, generate a reply draft, edit it, and approve sending.
9. Click **Zapísať do HubSpotu** on the reply to add the CRM timeline note.
10. Only after this succeeds, repeat with one real company.

## Work payload

The results endpoint accepts:

```json
{
  "results": [
    {
      "recipient_id": 123,
      "fit_score": 82,
      "fit_reason": "Concrete evidence-based reason.",
      "analysis": {
        "company_type": "Electrical contractor",
        "services": ["Industrial electrical installation"],
        "markets": ["Slovakia"],
        "works_abroad": false,
        "regions": ["Prešov region"],
        "employee_count": null,
        "employee_count_source": null,
        "subcontractor_need": "medium",
        "outreach_relevant": true,
        "analysis_reason": "Evidence-based summary.",
        "analysis_evidence": [
          {"quote": "Short source quote", "source_url": "https://example.com/services"}
        ]
      }
    }
  ]
}
```

The request is atomic: one invalid item rejects the whole batch.
