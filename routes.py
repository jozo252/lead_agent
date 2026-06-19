from datetime import datetime, date
from reply_generator import generate_reply_to_customer

from flask import Blueprint, render_template, request, redirect, url_for, flash
from models import Lead, LeadActivity, EmailReply
from extensions import db, mail, csrf
from ai_service import generate_lead_message, analyze_lead
from flask_mail import Message
from lead_finder_service import search_places_text, build_search_queries
from email_checker_service import check_reply_from_sender
from email_finder import find_email_on_website
from datetime import datetime
import requests
import os


main_bp = Blueprint("main", __name__)


WORK_TYPES = [
    "Elektro",
    "Stavebné práce",
    "Zváranie / kovovýroba",
    "Montáže",
    "Iné"
]


COMPANY_SEGMENTS = [
    "Kuchynské štúdio",
    "Rekonštrukčná firma",
    "Stavebná firma",
    "Správca bytov",
    "FVE / tepelné čerpadlá / klimatizácie",
    "Stolárstvo",
    "Interiérové štúdio",
    "Developer",
    "Priemyselná firma",
    "Iné"
]


LEAD_STATUSES = [
    "Nový",
    "Skontrolovať",
    "Osloviť",
    "Oslovený",
    "Odpovedal",
    "Telefonát",
    "Obhliadka",
    "Cenová ponuka",
    "Vyhraté",
    "Prehraté",
    "Nezaujímavé"
]

ACTIVITY_TYPES = [
    "Poznámka",
    "Email odoslaný",
    "Follow-up nastavený",
    "Telefonát",
    "Odpoveď",
    "Obhliadka",
    "Cenová ponuka",
    "Vyhraté",
    "Prehraté"
]

@main_bp.route("/", methods=["GET", "POST"])
def home():
    today = date.today()

    if request.method == "POST":
        company_name = request.form.get("company_name", "").strip()

        if not company_name:
            flash("Názov firmy je povinný.", "error")
            return redirect(url_for("main.home"))

        lead_score_raw = request.form.get("lead_score", "3")

        try:
            lead_score = int(lead_score_raw)
        except ValueError:
            lead_score = 3

        lead_score = max(1, min(5, lead_score))

        lead = Lead(
            company_name=company_name,
            website=request.form.get("website", "").strip(),
            email=request.form.get("email", "").strip(),
            phone=request.form.get("phone", "").strip(),
            city=request.form.get("city", "").strip(),
            country=request.form.get("country", "Slovensko").strip() or "Slovensko",
            company_segment=request.form.get("company_segment", "").strip(),
            work_type=request.form.get("work_type", "").strip(),
            work_subtype=request.form.get("work_subtype", "").strip(),
            lead_score=lead_score,
            status=request.form.get("status", "Nový").strip(),
            reason_to_contact=request.form.get("reason_to_contact", "").strip(),
            ai_summary=request.form.get("ai_summary", "").strip(),
            suggested_message=request.form.get("suggested_message", "").strip(),
        )

        db.session.add(lead)
        db.session.commit()

        flash("Lead bol pridaný.", "success")
        return redirect(url_for("main.home"))

    status_filter = request.args.get("status", "").strip()
    work_type_filter = request.args.get("work_type", "").strip()
    score_filter = request.args.get("score", "").strip()
    follow_up_filter = request.args.get("follow_up", "").strip()

    query = Lead.query

    if status_filter:
        query = query.filter(Lead.status == status_filter)

    if work_type_filter:
        query = query.filter(Lead.work_type == work_type_filter)

    if score_filter:
        try:
            min_score = int(score_filter)
            query = query.filter(Lead.lead_score >= min_score)
        except ValueError:
            pass

    if follow_up_filter == "today":
        query = query.filter(
            Lead.next_follow_up_at.isnot(None),
            db.func.date(Lead.next_follow_up_at) <= today
        )

    leads = query.order_by(Lead.created_at.desc()).all()
    last_activities = {}

    for lead in leads:
        last_activity = LeadActivity.query.filter_by(lead_id=lead.id)\
            .order_by(LeadActivity.created_at.desc())\
            .first()

        last_activities[lead.id] = last_activity

    email_sent_count = LeadActivity.query.filter_by(activity_type="Email odoslaný").count()
    reply_count = LeadActivity.query.filter_by(activity_type="Odpoveď").count()
    inspection_count = LeadActivity.query.filter_by(activity_type="Obhliadka").count()

    if email_sent_count > 0:
        reply_rate = round((reply_count / email_sent_count) * 100, 1)
    else:
        reply_rate = 0

    stats = {
        "total": Lead.query.count(),
        "new": Lead.query.filter_by(status="Nový").count(),
        "to_contact": Lead.query.filter_by(status="Osloviť").count(),
        "contacted": Lead.query.filter_by(status="Oslovený").count(),
        "won": Lead.query.filter_by(status="Vyhraté").count(),
        "follow_up_today": Lead.query.filter(
            Lead.next_follow_up_at.isnot(None),
            db.func.date(Lead.next_follow_up_at) <= today
        ).count(),
        "emails_sent": email_sent_count,
        "replies": reply_count,
        "inspections": inspection_count,
        "reply_rate": reply_rate,
    }

    return render_template(
        "home.html",
        leads=leads,
        stats=stats,
        work_types=WORK_TYPES,
        company_segments=COMPANY_SEGMENTS,
        lead_statuses=LEAD_STATUSES,
        last_activities=last_activities,
        filters={
            "status": status_filter,
            "work_type": work_type_filter,
            "score": score_filter,
            "follow_up": follow_up_filter,
        }
    )

@main_bp.route("/lead/<int:lead_id>/delete", methods=["POST"])
def delete_lead(lead_id):
    lead = Lead.query.get_or_404(lead_id)

    db.session.delete(lead)
    db.session.commit()

    flash("Lead bol vymazaný.", "success")
    return redirect(url_for("main.home"))


@main_bp.route("/lead/<int:lead_id>/status", methods=["POST"])
def update_lead_status(lead_id):
    lead = Lead.query.get_or_404(lead_id)

    new_status = request.form.get("status", "Nový")

    if new_status in LEAD_STATUSES:
        lead.status = new_status
        db.session.commit()
        flash("Stav leadu bol upravený.", "success")
    else:
        flash("Neplatný stav leadu.", "error")

    return redirect(url_for("main.home"))

@main_bp.route("/lead/<int:lead_id>/generate-message", methods=["POST"])
def generate_message(lead_id):
    lead = Lead.query.get_or_404(lead_id)

    try:
        message = generate_lead_message(lead)
        lead.suggested_message = message
        activity = LeadActivity(
            lead_id=lead.id,
            activity_type="Poznámka",
            note="AI vygenerovala návrh oslovenia."
        )

        db.session.add(activity)

        if not lead.status or lead.status == "Nový":
            lead.status = "Osloviť"

        db.session.commit()
        flash("Oslovenie bolo vygenerované.", "success")

    except Exception as e:
        db.session.rollback()
        flash(f"Chyba pri generovaní oslovenia: {str(e)}", "error")

    return redirect(request.referrer or url_for("main.home"))

@main_bp.route("/lead/<int:lead_id>/save-message", methods=["POST"])
def save_message(lead_id):
    lead = Lead.query.get_or_404(lead_id)

    message = request.form.get("suggested_message", "").strip()

    if not message:
        flash("Text správy nemôže byť prázdny.", "error")
        return redirect(url_for("main.home"))

    lead.suggested_message = message
    db.session.commit()

    flash("Text oslovenia bol uložený.", "success")
    return redirect(request.referrer or url_for("main.home"))


@main_bp.route("/lead/<int:lead_id>/send-email", methods=["POST"])
def send_email(lead_id):
    lead = Lead.query.get_or_404(lead_id)

    message_text = request.form.get("suggested_message", "").strip()
    subject = request.form.get("email_subject", "").strip()

    if not lead.email:
        flash("Lead nemá vyplnený email.", "error")
        return redirect(url_for("main.home"))

    if not message_text:
        flash("Text emailu nemôže byť prázdny.", "error")
        return redirect(request.referrer or url_for("main.home"))

    if not subject:
        subject = "Možnosť spolupráce"

    try:
        msg = Message(
            subject=subject,
            recipients=[lead.email],
            body=message_text
        )

        mail.send(msg)

        lead.suggested_message = message_text
        lead.status = "Oslovený"
        lead.last_contacted_at = datetime.utcnow()

        activity = LeadActivity(
            lead_id=lead.id,
            activity_type="Email odoslaný",
            note=f"Predmet: {subject}\n\n{message_text}"
        )

        db.session.add(activity)
        db.session.commit()

        flash("Email bol odoslaný a lead označený ako oslovený.", "success")

    except Exception as e:
        db.session.rollback()
        flash(f"Email sa nepodarilo odoslať: {str(e)}", "error")

    return redirect(url_for("main.home"))

@main_bp.route("/find-leads", methods=["POST"])
def find_leads():
    locations_raw = request.form.get("locations", "")
    company_types_raw = request.form.get("company_types", "")
    work_type = request.form.get("work_type", "").strip()
    work_subtype = request.form.get("work_subtype", "").strip()
    max_results_raw = request.form.get("max_results", "5")

    try:
        max_results = int(max_results_raw)
    except ValueError:
        max_results = 5

    max_results = max(1, min(20, max_results))

    locations = [item.strip() for item in locations_raw.split(",") if item.strip()]
    company_types = [item.strip() for item in company_types_raw.split(",") if item.strip()]

    if not locations:
        flash("Zadaj aspoň jednu lokalitu.", "error")
        return redirect(url_for("main.home"))

    if not company_types:
        flash("Zadaj aspoň jeden typ firmy.", "error")
        return redirect(url_for("main.home"))

    queries = build_search_queries(locations, company_types)

    created_count = 0
    skipped_count = 0

    for query in queries:
        try:
            places = search_places_text(query, max_results=max_results)

            for place in places:
                google_place_id = place.get("google_place_id")

                if not google_place_id:
                    skipped_count += 1
                    continue

                existing = Lead.query.filter_by(google_place_id=google_place_id).first()

                if existing:
                    skipped_count += 1
                    continue

                lead = Lead(
                    google_place_id=google_place_id,
                    company_name=place.get("company_name") or "Neznáma firma",
                    website=place.get("website"),
                    phone=place.get("phone"),
                    address=place.get("address"),
                    business_status=place.get("business_status"),
                    source="Google Places",
                    city=", ".join(locations),
                    country="Slovensko",
                    company_segment=query,
                    work_type=work_type,
                    work_subtype=work_subtype,
                    status="Nový",
                    lead_score=3,
                    reason_to_contact=(
                        f"Firma bola nájdená podľa dotazu: {query}. "
                        f"Skontrolovať, či dáva zmysel osloviť ju s ponukou: {work_type} / {work_subtype}."
                    )
                )

                db.session.add(lead)
                created_count += 1

        except Exception as e:
            flash(f"Chyba pri hľadaní pre dotaz '{query}': {str(e)}", "error")

    db.session.commit()

    flash(f"Hľadanie dokončené. Pridané: {created_count}, preskočené duplicity/neplatné: {skipped_count}.", "success")
    return redirect(url_for("main.home"))

@main_bp.route("/lead/<int:lead_id>", methods=["GET"])
def lead_detail(lead_id):
    lead = Lead.query.get_or_404(lead_id)

    activities = LeadActivity.query.filter_by(lead_id=lead.id)\
        .order_by(LeadActivity.created_at.desc())\
        .all()
    
    email_replies = EmailReply.query.filter_by(lead_id=lead.id)\
        .order_by(EmailReply.created_at.desc())\
        .all()

    return render_template(
        "lead_detail.html",
        lead=lead,
        activities=activities,
        activity_types=ACTIVITY_TYPES,
        work_types=WORK_TYPES,
        company_segments=COMPANY_SEGMENTS,
        lead_statuses=LEAD_STATUSES,
        email_replies=email_replies
    )


@main_bp.route("/lead/<int:lead_id>/analyze", methods=["POST"])
def analyze_lead_route(lead_id):
    lead = Lead.query.get_or_404(lead_id)

    try:
        data = analyze_lead(lead)

        lead.company_segment = data.get("company_segment") or lead.company_segment
        lead.work_type = data.get("work_type") or lead.work_type
        lead.work_subtype = data.get("work_subtype") or lead.work_subtype
        lead.reason_to_contact = data.get("reason_to_contact") or lead.reason_to_contact
        lead.ai_summary = data.get("ai_summary") or lead.ai_summary

        score = data.get("lead_score", lead.lead_score)

        try:
            score = int(score)
        except ValueError:
            score = lead.lead_score or 3

        lead.lead_score = max(1, min(5, score))

        if not lead.status or lead.status == "Nový":
            lead.status = "Skontrolovať"
        activity = LeadActivity(
            lead_id=lead.id,
            activity_type="Poznámka",
            note="Lead bol vyhodnotený cez AI."
        )

        db.session.add(activity)
        db.session.commit()
        flash("Lead bol vyhodnotený cez AI.", "success")

    except Exception as e:
        db.session.rollback()
        flash(f"Chyba pri AI vyhodnotení leadu: {str(e)}", "error")

    return redirect(url_for("main.lead_detail", lead_id=lead.id))

@main_bp.route("/lead/<int:lead_id>/update-basic", methods=["POST"])
def update_lead_basic(lead_id):
    lead = Lead.query.get_or_404(lead_id)

    lead.email = request.form.get("email", "").strip()
    lead.phone = request.form.get("phone", "").strip()
    lead.company_segment = request.form.get("company_segment", "").strip()
    lead.work_type = request.form.get("work_type", "").strip()
    lead.work_subtype = request.form.get("work_subtype", "").strip()

    score_raw = request.form.get("lead_score", "3")

    try:
        score = int(score_raw)
    except ValueError:
        score = 3

    lead.lead_score = max(1, min(5, score))

    db.session.commit()

    flash("Lead bol upravený.", "success")
    return redirect(request.referrer or url_for("main.lead_detail", lead_id=lead.id))


@main_bp.route("/lead/<int:lead_id>/set-follow-up", methods=["POST"])
def set_follow_up(lead_id):
    lead = Lead.query.get_or_404(lead_id)

    follow_up_date_raw = request.form.get("next_follow_up_at", "").strip()

    if not follow_up_date_raw:
        lead.next_follow_up_at = None
        db.session.commit()
        flash("Follow-up bol odstránený.", "success")
        return redirect(request.referrer or url_for("main.home"))

    try:
        follow_up_date = datetime.strptime(follow_up_date_raw, "%Y-%m-%d")
    except ValueError:
        flash("Neplatný dátum follow-upu.", "error")
        return redirect(request.referrer or url_for("main.home"))

    lead.next_follow_up_at = follow_up_date
    activity = LeadActivity(
        lead_id=lead.id,
        activity_type="Follow-up nastavený",
        note=f"Follow-up nastavený na {follow_up_date.strftime('%d.%m.%Y')}"
    )

    db.session.add(activity)

    if lead.status in ["Nový", "Oslovený"]:
        lead.status = "Oslovený"

    db.session.commit()

    flash("Follow-up bol nastavený.", "success")
    return redirect(request.referrer or url_for("main.home"))

@main_bp.route("/lead/<int:lead_id>/add-activity", methods=["POST"])
def add_activity(lead_id):
    lead = Lead.query.get_or_404(lead_id)

    activity_type = request.form.get("activity_type", "Poznámka").strip()
    note = request.form.get("note", "").strip()

    if not note:
        flash("Poznámka aktivity nemôže byť prázdna.", "error")
        return redirect(url_for("main.lead_detail", lead_id=lead.id))

    if activity_type not in ACTIVITY_TYPES:
        activity_type = "Poznámka"

    activity = LeadActivity(
        lead_id=lead.id,
        activity_type=activity_type,
        note=note
    )

    db.session.add(activity)
    db.session.commit()

    flash("Aktivita bola pridaná.", "success")
    return redirect(url_for("main.lead_detail", lead_id=lead.id))




@main_bp.route("/lead/<int:lead_id>/check-reply", methods=["POST"])
def check_reply(lead_id):
    lead = Lead.query.get_or_404(lead_id)

    if not lead.email:
        flash("Lead nemá email, nemám podľa čoho hľadať odpoveď.", "error")
        return redirect(request.referrer or url_for("main.lead_detail", lead_id=lead.id))

    try:
        reply = check_reply_from_sender(
            sender_email=lead.email,
            after_datetime=lead.last_contacted_at
        )

        if not reply:
            flash("Zatiaľ som nenašiel odpoveď od tejto firmy.", "error")
            return redirect(request.referrer or url_for("main.lead_detail", lead_id=lead.id))

        subject = reply.get("subject", "")
        received_at = reply.get("received_at")
        body = reply.get("body", "")

        note = f"Predmet: {subject}\n"

        if received_at:
            note += f"Doručené: {received_at.strftime('%d.%m.%Y %H:%M')}\n"

        note += f"\n{body}"

        activity = LeadActivity(
            lead_id=lead.id,
            activity_type="Odpoveď",
            note=note
        )

        lead.status = "Odpovedal"

        db.session.add(activity)
        db.session.commit()

        flash("Našiel som odpoveď a zapísal ju do histórie.", "success")

    except Exception as e:
        db.session.rollback()
        flash(f"Kontrola odpovede zlyhala: {str(e)}", "error")

    return redirect(request.referrer or url_for("main.lead_detail", lead_id=lead.id))


@main_bp.route("/lead/<int:lead_id>/find-email", methods=["POST"])
def find_lead_email(lead_id):
    lead = Lead.query.get_or_404(lead_id)

    if lead.email:
        flash("Lead už má email.", "info")
        return redirect(url_for("main.home"))

    if not lead.website:
        flash("Lead nemá web, email sa nedá automaticky hľadať.", "warning")
        return redirect(url_for("main.home"))

    best_email, all_emails = find_email_on_website(lead.website)

    if best_email:
        lead.email = best_email

        if hasattr(lead, "ai_summary"):
            existing_summary = lead.ai_summary or ""
            lead.ai_summary = (
                existing_summary
                + f"\n\nNájdené emaily: {', '.join(all_emails)}"
            ).strip()

        db.session.commit()

        flash(f"Email nájdený: {best_email}", "success")
    else:
        flash("Email sa na webe nepodarilo nájsť.", "warning")

    return redirect(url_for("main.home"))



@main_bp.route("/leads/find-missing-emails", methods=["POST"])
def find_missing_emails():
    leads = Lead.query.filter(
        Lead.email.is_(None),
        Lead.website.isnot(None)
    ).limit(20).all()

    found_count = 0

    for lead in leads:
        best_email, all_emails = find_email_on_website(lead.website)

        if best_email:
            lead.email = best_email
            found_count += 1

            if hasattr(lead, "ai_summary"):
                existing_summary = lead.ai_summary or ""
                lead.ai_summary = (
                    existing_summary
                    + f"\n\nNájdené emaily: {', '.join(all_emails)}"
                ).strip()

    db.session.commit()

    flash(f"Nájdených emailov: {found_count}", "success")
    return redirect(url_for("main.home"))




#___________________________________________INBOUND EMAIL CHECKER - TESTOVACIA ROUTA___________________________________________
@main_bp.route("/postmark/inbound", methods=["POST"])
@csrf.exempt
def postmark_inbound():
    data = request.get_json(silent=True) or {}

    from_email = data.get("From")
    from_name = data.get("FromName")
    subject = data.get("Subject")
    text_body = data.get("StrippedTextReply") or data.get("TextBody")
    html_body = data.get("HtmlBody")
    postmark_message_id = data.get("MessageID")
    mailbox_hash = data.get("MailboxHash")

    lead = None

    if from_email:
        lead = Lead.query.filter(
            Lead.email.ilike(from_email.strip())
        ).first()

    reply = EmailReply(
        lead_id=lead.id if lead else None,
        from_email=from_email,
        from_name=from_name,
        subject=subject,
        text_body=text_body,
        html_body=html_body,
        postmark_message_id=postmark_message_id,
        mailbox_hash=mailbox_hash,
    )

    db.session.add(reply)

    if lead:
        lead.status = "Odpovedal"

    db.session.commit()

    return "OK", 200


@main_bp.route("/reply/<int:reply_id>/generate", methods=["POST"])
def generate_reply(reply_id):
    reply = EmailReply.query.get_or_404(reply_id)

    if not reply.lead:
        flash("Odpoveď nie je priradená k žiadnemu leadu.", "error")
        return redirect(url_for("main.home"))

    draft = generate_reply_to_customer(reply.lead, reply)

    reply.ai_reply_draft = draft
    db.session.commit()

    flash("Návrh odpovede bol vygenerovaný.", "success")
    return redirect(url_for("main.lead_detail", lead_id=reply.lead.id))





@main_bp.route("/reply/<int:reply_id>/send", methods=["POST"])
def send_reply(reply_id):
    reply = EmailReply.query.get_or_404(reply_id)

    if not reply.lead:
        flash("Odpoveď nie je priradená k leadu.", "error")
        return redirect(url_for("main.home"))

    reply_body = request.form.get("reply_body")

    if not reply_body:
        flash("Text odpovede je prázdny.", "error")
        return redirect(url_for("main.lead_detail", lead_id=reply.lead.id))

    subject = reply.subject or "Re: Spolupráca"

    if not subject.lower().startswith("re:"):
        subject = "Re: " + subject

    response = requests.post(
        "https://api.postmarkapp.com/email",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Postmark-Server-Token": os.getenv("POSTMARK_SERVER_TOKEN"),
        },
        json={
            "From": os.getenv("POSTMARK_FROM_EMAIL"),
            "To": reply.from_email,
            "Subject": subject,
            "TextBody": reply_body,
        },
        timeout=10,
    )

    if response.status_code >= 400:
        flash(f"Nepodarilo sa odoslať odpoveď: {response.text}", "error")
        return redirect(url_for("main.lead_detail", lead_id=reply.lead.id))

    reply.ai_reply_draft = reply_body
    reply.reply_sent_at = datetime.utcnow()

    reply.lead.status = "Odpovedané"

    db.session.commit()

    flash("Odpoveď bola odoslaná.", "success")
    return redirect(url_for("main.lead_detail", lead_id=reply.lead.id))




@main_bp.route("/inbox")
def inbox():
    emails = EmailReply.query.order_by(EmailReply.received_at.desc()).all()

    return render_template(
        "inbox.html",
        emails=emails,
        selected_email=None
    )


@main_bp.route("/inbox/<int:email_id>")
def inbox_detail(email_id):
    emails = EmailReply.query.order_by(EmailReply.received_at.desc()).all()
    selected_email = EmailReply.query.get_or_404(email_id)

    return render_template(
        "inbox.html",
        emails=emails,
        selected_email=selected_email
    )


