import hmac

from flask import Blueprint, current_app, jsonify, request

from extensions import csrf, db
from models import Campaign, CampaignRecipient
from services.company_web_enrichment import (
    apply_company_analysis,
    normalize_company_analysis,
)


integration_bp = Blueprint("integrations", __name__, url_prefix="/integrations")


def _authorized():
    configured = current_app.config.get("WORK_API_TOKEN")
    supplied = request.headers.get("X-Work-Token", "")
    return bool(configured and supplied and hmac.compare_digest(configured, supplied))


def _recipient_payload(recipient):
    company = recipient.company
    return {
        "recipient_id": recipient.id,
        "status": recipient.status,
        "fit_score": recipient.fit_score,
        "fit_reason": recipient.fit_reason,
        "subject": recipient.subject,
        "body": recipient.body,
        "company": {
            "id": company.id,
            "ico": company.ico,
            "official_name": company.official_name,
            "status": company.status,
            "legal_form": company.legal_form,
            "sk_nace_code": company.sk_nace_code,
            "sk_nace_name": company.sk_nace_name,
            "municipality": company.municipality,
            "postal_code": company.postal_code,
            "company_type": company.company_type,
            "services": company.services,
            "markets": company.markets,
            "works_abroad": company.works_abroad,
            "regions": company.regions,
            "employee_count": company.employee_count,
            "subcontractor_need": company.subcontractor_need,
            "outreach_relevant": company.outreach_relevant,
            "analysis_reason": company.analysis_reason,
            "analysis_evidence": company.analysis_evidence,
            "activities": [activity.description for activity in company.activities],
            "contacts": [
                {
                    "type": contact.contact_type,
                    "value": contact.value,
                    "verified": contact.is_verified,
                    "source_url": contact.source_url,
                    "confidence_score": contact.confidence_score,
                }
                for contact in company.contacts
            ],
        },
    }


@integration_bp.route("/work/campaigns/<int:campaign_id>/batch")
@csrf.exempt
def work_campaign_batch(campaign_id):
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401

    campaign = Campaign.query.get_or_404(campaign_id)
    limit = request.args.get("limit", default=25, type=int)
    limit = max(1, min(limit or 25, 100))
    recipients = (
        CampaignRecipient.query.filter_by(campaign_id=campaign.id, status="draft")
        .order_by(CampaignRecipient.id)
        .limit(limit)
        .all()
    )
    return jsonify({
        "campaign": {
            "id": campaign.id,
            "name": campaign.name,
            "offer_type": campaign.offer_type,
            "offer_description": campaign.offer_description,
            "targeting_profile": campaign.targeting_profile,
        },
        "recipients": [_recipient_payload(recipient) for recipient in recipients],
    })


@integration_bp.route(
    "/work/campaigns/<int:campaign_id>/results",
    methods=["POST"],
)
@csrf.exempt
def apply_work_campaign_results(campaign_id):
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401

    campaign = Campaign.query.get_or_404(campaign_id)
    payload = request.get_json(silent=True) or {}
    results = payload.get("results")
    if not isinstance(results, list) or not results:
        return jsonify({"error": "results must be a non-empty list"}), 400

    updated = []
    try:
        for item in results:
            if not isinstance(item, dict):
                raise ValueError("Každý výsledok musí byť JSON objekt.")
            recipient_id = item.get("recipient_id")
            recipient = CampaignRecipient.query.filter_by(
                id=recipient_id,
                campaign_id=campaign.id,
            ).one_or_none()
            if recipient is None:
                raise ValueError(f"Neznámy recipient_id: {recipient_id}")
            if recipient.status != "draft":
                raise ValueError(
                    f"Príjemca {recipient.id} už nie je koncept a nemožno ho prepísať."
                )

            fit_score = item.get("fit_score")
            if not isinstance(fit_score, int) or not 0 <= fit_score <= 100:
                raise ValueError("fit_score musí byť celé číslo od 0 do 100.")
            fit_reason = str(item.get("fit_reason") or "").strip()
            if not fit_reason:
                raise ValueError("fit_reason je povinný.")

            recipient.fit_score = fit_score
            recipient.fit_reason = fit_reason[:2000]
            recipient.selection_source = "chatgpt_work"
            analysis = item.get("analysis")
            if analysis is not None:
                apply_company_analysis(
                    recipient.company,
                    normalize_company_analysis(analysis),
                )
            updated.append(recipient.id)

        db.session.commit()
    except (TypeError, ValueError) as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 400

    return jsonify({"updated_recipient_ids": updated, "status": "draft"})
