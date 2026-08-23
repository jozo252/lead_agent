import secrets
from datetime import datetime, timezone
from email.utils import parseaddr

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
)

from extensions import db
from models import Campaign, LandingPage
from services.campaign_ai import CampaignAIError, generate_landing_page_copy
from services.landing_pages import (
    available_slug,
    configured_operator_identity,
    configured_landing_page_url,
    default_landing_page_content,
    missing_operator_config,
    normalize_landing_page_content,
    safe_image_url,
    valid_contact_email,
    validation_notice_for,
)


landing_page_bp = Blueprint("landing_pages", __name__)


def utcnow():
    return datetime.now(timezone.utc)


def configured_sender_email():
    sender = current_app.config.get("MAIL_DEFAULT_SENDER") or ""
    if isinstance(sender, (tuple, list)) and len(sender) > 1:
        sender = sender[1]
    return valid_contact_email(parseaddr(str(sender))[1])


def content_from_form():
    fields = (
        "brand_name",
        "eyebrow",
        "headline",
        "subheadline",
        "problem_title",
        "problem_text",
        "solution_title",
        "solution_text",
        "process_title",
        "audience_title",
        "audience_text",
        "cta_title",
        "cta_text",
        "cta_label",
        "meta_description",
    )
    content = {field: request.form.get(field, "") for field in fields}
    content["benefits"] = [
        {
            "title": request.form.get(f"benefit_title_{index}", ""),
            "text": request.form.get(f"benefit_text_{index}", ""),
        }
        for index in range(1, 4)
    ]
    content["steps"] = [
        {
            "title": request.form.get(f"step_title_{index}", ""),
            "text": request.form.get(f"step_text_{index}", ""),
        }
        for index in range(1, 4)
    ]
    return content


@landing_page_bp.route(
    "/campaigns/<int:campaign_id>/landing-page/create",
    methods=["POST"],
)
def create_landing_page(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.landing_page is not None:
        return redirect(
            url_for("landing_pages.edit_landing_page", campaign_id=campaign.id)
        )

    content = default_landing_page_content(campaign)
    generated_by_ai = False
    try:
        content = normalize_landing_page_content(
            generate_landing_page_copy(campaign),
            campaign,
        )
        generated_by_ai = True
    except CampaignAIError as exc:
        flash(
            f"AI návrh sa nepodaril ({exc}). Vytvoril sa upraviteľný základ.",
            "warning",
        )
    except Exception:
        current_app.logger.exception("AI generation of landing page failed")
        flash(
            "AI návrh sa nepodaril. Vytvoril sa upraviteľný základ.",
            "warning",
        )

    landing_page = LandingPage(
        campaign=campaign,
        slug=available_slug(campaign.name),
        preview_token=secrets.token_urlsafe(32),
        status="draft",
        content=content,
        contact_email=configured_sender_email(),
    )
    db.session.add(landing_page)
    db.session.commit()
    if generated_by_ai:
        flash("AI pripravila koncept landing page. Pred publikovaním ho skontroluj.", "success")
    return redirect(
        url_for("landing_pages.edit_landing_page", campaign_id=campaign.id)
    )


@landing_page_bp.route("/campaigns/<int:campaign_id>/landing-page")
def edit_landing_page(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    landing_page = campaign.landing_page
    if landing_page is None:
        flash("Kampaň ešte nemá landing page.", "warning")
        return redirect(url_for("campaigns.campaign_detail", campaign_id=campaign.id))
    public_url = configured_landing_page_url(landing_page) or url_for(
        "landing_pages.public_landing_page",
        slug=landing_page.slug,
        _external=True,
    )
    preview_url = f"{public_url}?preview={landing_page.preview_token}"
    return render_template(
        "landing_page_editor.html",
        campaign=campaign,
        landing_page=landing_page,
        content=landing_page.content or {},
        preview_url=preview_url,
        public_url=public_url,
        operator_identity=configured_operator_identity(),
        missing_operator_config=missing_operator_config(),
    )


@landing_page_bp.route(
    "/campaigns/<int:campaign_id>/landing-page/update",
    methods=["POST"],
)
def update_landing_page(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    landing_page = campaign.landing_page
    if landing_page is None:
        abort(404)

    action = request.form.get("action", "save")
    if action not in {"save", "publish", "unpublish"}:
        flash("Neplatná akcia landing page.", "error")
        return redirect(
            url_for("landing_pages.edit_landing_page", campaign_id=campaign.id)
        )

    image_input = request.form.get("hero_image_url", "").strip()
    image_url = safe_image_url(image_input)
    if image_input and not image_url:
        flash("Obrázok musí používať HTTPS adresu alebo cestu /static/.", "error")
        return redirect(
            url_for("landing_pages.edit_landing_page", campaign_id=campaign.id)
        )

    email_input = request.form.get("contact_email", "").strip()
    contact_email = valid_contact_email(email_input)
    if email_input and not contact_email:
        flash("Zadaj platný kontaktný e-mail.", "error")
        return redirect(
            url_for("landing_pages.edit_landing_page", campaign_id=campaign.id)
        )
    if action == "publish" and not contact_email:
        flash("Pred publikovaním doplň kontaktný e-mail.", "error")
        return redirect(
            url_for("landing_pages.edit_landing_page", campaign_id=campaign.id)
        )

    missing_operator = missing_operator_config()
    if action == "publish" and missing_operator:
        flash(
            "Pred publikovaním doplň produkčnú identitu prevádzkovateľa: "
            + ", ".join(missing_operator)
            + ".",
            "error",
        )
        return redirect(
            url_for("landing_pages.edit_landing_page", campaign_id=campaign.id)
        )

    landing_page.slug = available_slug(
        request.form.get("slug", campaign.name),
        landing_page_id=landing_page.id,
    )
    landing_page.content = normalize_landing_page_content(
        content_from_form(),
        campaign,
    )
    landing_page.hero_image_url = image_url or None
    landing_page.hero_image_alt = request.form.get("hero_image_alt", "").strip()[:255] or None
    landing_page.contact_email = contact_email or None
    if action == "publish":
        landing_page.status = "published"
        landing_page.published_at = landing_page.published_at or utcnow()
        flash("Landing page bola publikovaná.", "success")
    elif action == "unpublish":
        landing_page.status = "draft"
        flash("Landing page bola stiahnutá z verejného odkazu.", "success")
    else:
        flash("Koncept landing page bol uložený.", "success")
    db.session.commit()
    return redirect(
        url_for("landing_pages.edit_landing_page", campaign_id=campaign.id)
    )


@landing_page_bp.route("/ponuka/<slug>")
def public_landing_page(slug):
    landing_page = LandingPage.query.filter_by(slug=slug).first_or_404()
    preview_token = request.args.get("preview", "")
    is_preview = bool(preview_token) and secrets.compare_digest(
        preview_token,
        landing_page.preview_token,
    )
    if landing_page.status != "published" and not is_preview:
        abort(404)
    operator_identity = configured_operator_identity()
    if not is_preview and missing_operator_config():
        current_app.logger.error(
            "Published landing page %s is missing operator configuration",
            landing_page.slug,
        )
        abort(503)
    response = make_response(
        render_template(
            "landing_page_public.html",
            campaign=landing_page.campaign,
            landing_page=landing_page,
            content=landing_page.content or {},
            validation_notice=validation_notice_for(landing_page.campaign),
            is_preview=is_preview,
            operator_identity=operator_identity,
        )
    )
    if is_preview:
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; style-src 'unsafe-inline'; img-src https: data:; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )
    response.headers["Permissions-Policy"] = (
        "camera=(), microphone=(), geolocation=()"
    )
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    return response
