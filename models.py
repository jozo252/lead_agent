from datetime import datetime, timezone

from sqlalchemy import Index, UniqueConstraint
from extensions import db

def utcnow() -> datetime:
    return datetime.now(timezone.utc)

class Lead(db.Model):
    id = db.Column(db.Integer, primary_key=True)

    company_id = db.Column(
        db.Integer,
        db.ForeignKey("companies.id"),
        unique=True,
        nullable=True,
    )

    company_name = db.Column(db.String(200), nullable=False)
    website = db.Column(db.String(300))
    email = db.Column(db.String(200))
    phone = db.Column(db.String(100))

    google_place_id = db.Column(db.String(200), unique=True)
    address = db.Column(db.String(300))
    source = db.Column(db.String(100), default="Manual")
    business_status = db.Column(db.String(100))

    city = db.Column(db.String(100))
    country = db.Column(db.String(100), default="Slovensko")

    company_segment = db.Column(db.String(100))
    work_type = db.Column(db.String(100))
    work_subtype = db.Column(db.String(150))

    lead_score = db.Column(db.Integer, default=3)
    status = db.Column(db.String(50), default="Nový")

    reason_to_contact = db.Column(db.Text)
    ai_summary = db.Column(db.Text)
    suggested_message = db.Column(db.Text)

    last_contacted_at = db.Column(db.DateTime)
    next_follow_up_at = db.Column(db.DateTime)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<Lead {self.company_name}>"


class LeadActivity(db.Model):
    id = db.Column(db.Integer, primary_key=True)

    lead_id = db.Column(db.Integer, db.ForeignKey("lead.id"), nullable=False)
    activity_type = db.Column(db.String(50))
    note = db.Column(db.Text)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    lead = db.relationship("Lead", backref="activities")

    def __repr__(self):
        return f"<LeadActivity {self.activity_type}>"
    

class EmailReply(db.Model):
    id = db.Column(db.Integer, primary_key=True)

    lead_id = db.Column(db.Integer, db.ForeignKey("lead.id"), nullable=True)
    campaign_recipient_id = db.Column(
        db.Integer,
        db.ForeignKey("campaign_recipients.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    from_email = db.Column(db.String(255), nullable=True)
    from_name = db.Column(db.String(255), nullable=True)
    subject = db.Column(db.String(255), nullable=True)

    text_body = db.Column(db.Text, nullable=True)
    html_body = db.Column(db.Text, nullable=True)

    postmark_message_id = db.Column(db.String(255), nullable=True)
    imap_message_id = db.Column(db.String(255), unique=True, nullable=True)
    mailbox_hash = db.Column(db.String(255), nullable=True)
    ai_reply_draft = db.Column(db.Text, nullable=True)
    reply_sent_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    received_at = db.Column(db.DateTime, default=datetime.utcnow)

    lead = db.relationship("Lead", backref="email_replies")
    campaign_recipient = db.relationship(
        "CampaignRecipient",
        back_populates="email_replies",
    )


class OutboundEmail(db.Model):
    __tablename__ = "outbound_emails"

    id = db.Column(db.Integer, primary_key=True)
    lead_id = db.Column(
        db.Integer,
        db.ForeignKey("lead.id"),
        nullable=False,
        index=True,
    )
    campaign_recipient_id = db.Column(
        db.Integer,
        db.ForeignKey("campaign_recipients.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    message_id = db.Column(db.String(255), unique=True, nullable=False)
    recipient = db.Column(db.String(255), nullable=False)
    subject = db.Column(db.String(255), nullable=False)
    body = db.Column(db.Text, nullable=False)
    sent_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    lead = db.relationship("Lead", backref="outbound_emails")
    campaign_recipient = db.relationship(
        "CampaignRecipient",
        back_populates="outbound_emails",
    )


class QuoteRequest(db.Model):
    __tablename__ = "quote_requests"

    id = db.Column(db.Integer, primary_key=True)
    lead_id = db.Column(
        db.Integer,
        db.ForeignKey("lead.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    email_reply_id = db.Column(
        db.Integer,
        db.ForeignKey("email_reply.id", ondelete="SET NULL"),
        nullable=True,
        unique=True,
    )
    outbound_email_id = db.Column(
        db.Integer,
        db.ForeignKey("outbound_emails.id", ondelete="SET NULL"),
        nullable=True,
        unique=True,
    )
    recipient_email = db.Column(db.String(255), nullable=False)
    request_text = db.Column(db.Text, nullable=True)
    status = db.Column(
        db.String(30), nullable=False, default="awaiting_price", index=True
    )
    amount = db.Column(db.Numeric(12, 2), nullable=True)
    currency = db.Column(db.String(3), nullable=True)
    unit = db.Column(db.String(50), nullable=True)
    vat_text = db.Column(db.String(100), nullable=True)
    terms = db.Column(db.Text, nullable=True)
    validity_days = db.Column(db.Integer, nullable=True)
    sender_signature = db.Column(db.String(255), nullable=True)
    subject = db.Column(db.String(255), nullable=True)
    body = db.Column(db.Text, nullable=True)
    approval_token = db.Column(db.String(64), nullable=True, unique=True)
    approved_at = db.Column(db.DateTime, nullable=True)
    sending_started_at = db.Column(db.DateTime, nullable=True)
    sent_at = db.Column(db.DateTime, nullable=True)
    last_error = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    lead = db.relationship("Lead", backref="quote_requests")
    email_reply = db.relationship(
        "EmailReply",
        backref=db.backref("quote_request", uselist=False),
    )
    outbound_email = db.relationship("OutboundEmail")



class Company(db.Model):
    __tablename__ = "companies"
   

    id = db.Column(db.Integer, primary_key=True)

    ico = db.Column(
        db.String(20),
        unique=True,
        nullable=True,
        index=True,
    )

    official_name = db.Column(db.String(500), nullable=True, index=True)
    status = db.Column(db.String(100), nullable=True)
    legal_form = db.Column(db.String(255), nullable=True)
    sk_nace_code = db.Column(db.String(20), nullable=True, index=True)
    sk_nace_name = db.Column(db.String(500), nullable=True)
    employee_count = db.Column(db.Integer, nullable=True)
    employee_count_source = db.Column(db.String(100), nullable=True)

    company_type = db.Column(db.String(255), nullable=True, index=True)
    services = db.Column(db.JSON, nullable=True)
    markets = db.Column(db.JSON, nullable=True)
    works_abroad = db.Column(db.Boolean, nullable=True)
    regions = db.Column(db.JSON, nullable=True)
    subcontractor_need = db.Column(db.String(20), nullable=True)
    outreach_relevant = db.Column(db.Boolean, nullable=True, index=True)
    analysis_reason = db.Column(db.Text, nullable=True)
    analysis_evidence = db.Column(db.JSON, nullable=True)
    website_analyzed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    contacts_checked_at = db.Column(db.DateTime(timezone=True), nullable=True)

    financial_year = db.Column(db.Integer, nullable=True, index=True)
    annual_revenue = db.Column(db.Numeric(20, 2), nullable=True, index=True)
    annual_total_income = db.Column(db.Numeric(20, 2), nullable=True)
    annual_profit = db.Column(db.Numeric(20, 2), nullable=True)
    financial_statement_submitted_on = db.Column(db.Date, nullable=True)
    financials_status = db.Column(db.String(40), nullable=True, index=True)
    financials_checked_at = db.Column(db.DateTime(timezone=True), nullable=True)
    ruz_accounting_entity_id = db.Column(db.Integer, nullable=True)
    financial_statement_id = db.Column(db.Integer, nullable=True)
    financial_report_id = db.Column(db.Integer, nullable=True)
    financial_source_url = db.Column(db.Text, nullable=True)

    municipality = db.Column(db.String(255), nullable=True, index=True)
    postal_code = db.Column(db.String(20), nullable=True)
    street = db.Column(db.String(500), nullable=True)
    country = db.Column(db.String(100), nullable=True)

    established_on = db.Column(db.Date, nullable=True)
    terminated_on = db.Column(db.Date, nullable=True)

    rpo_actualized_at = db.Column(
        db.DateTime(timezone=True),
        nullable=True,
    )

    rpo_updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=True,
    )

    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utcnow,
    )

    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
    )
    activities = db.relationship(
        "CompanyActivity",
        back_populates="company",
        cascade="all, delete-orphan",
        lazy="selectin",
)
    sources = db.relationship(
        "CompanySource",
        back_populates="company",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    contacts = db.relationship(
        "CompanyContact",
        back_populates="company",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    campaign_recipients = db.relationship(
        "CampaignRecipient",
        back_populates="company",
        cascade="all, delete-orphan",
    )
    def __repr__(self) -> str:
        return f"<Company ico={self.ico!r} official_name={self.official_name!r}>"

class CompanySource(db.Model):
    """
    Pôvodný záznam z externého zdroja.

    Raw JSON je dôležitý, pretože štruktúra RPO2 sa môže meniť
    a neskôr z neho môžeme vytiahnuť ďalšie údaje.
    """

    __tablename__ = "company_sources"
    __table_args__ = (
        db.UniqueConstraint(
            "company_id",
            "source_id",
            name="uq_company_source",
        ),
    )
   # __table_args__ = (
   #     UniqueConstraint(
    #        "source_type",
    #        "external_id",
     #       name="uq_company_source_external_record",
     #   ),
   # )

    id = db.Column(db.Integer, primary_key=True)

    company_id = db.Column(
        db.Integer,
        db.ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    source_type = db.Column(
        db.String(50),
        nullable=False,
        default="rpo2",
        index=True,
    )
    source_id = db.Column(db.String(255), nullable=False)

    external_id = db.Column(db.String(100), nullable=False, index=True)

    resource_url = db.Column(db.Text, nullable=True)

    raw_data = db.Column(db.JSON, nullable=False)

    source_updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=True,
    )

    fetched_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utcnow,
    )

    company = db.relationship(
        "Company",
        back_populates="sources",
    )

    def __repr__(self) -> str:
        return (
            f"<CompanySource source={self.source_type!r} "
            f"external_id={self.external_id!r}>"
        )


class SyncState(db.Model):
    """
    Stav jedného synchronizačného procesu.

    next_url umožňuje pokračovať z konkrétnej stránky.
    sync_started_at zostáva rovnaký počas celého jedného behu.
    """

    __tablename__ = "sync_states"

    id = db.Column(db.Integer, primary_key=True)

    name = db.Column(db.String(100), unique=True, nullable=False)

    status = db.Column(
        db.String(30),
        nullable=False,
        default="idle",
    )

    # Posledná úplne dokončená synchronizácia.
    last_successful_sync_at = db.Column(
        db.DateTime(timezone=True),
        nullable=True,
    )

    # Čas, ktorý sme použili ako parameter since.
    sync_started_at = db.Column(
        db.DateTime(timezone=True),
        nullable=True,
    )

    # Presná URL ďalšej stránky.
    next_url = db.Column(db.Text, nullable=True)

    processed_records = db.Column(
        db.Integer,
        nullable=False,
        default=0,
    )

    fetched_records = db.Column(
        db.Integer,
        nullable=False,
        default=0,
    )

    skipped_records = db.Column(
        db.Integer,
        nullable=False,
        default=0,
    )

    last_error = db.Column(db.Text, nullable=True)

    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utcnow,
    )

    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
    )

    def __repr__(self) -> str:
        return f"<SyncState name={self.name!r} status={self.status!r}>"
    


class CompanyActivity(db.Model):
    __tablename__ = "company_activities"

    id = db.Column(db.Integer, primary_key=True)

    company_id = db.Column(
        db.Integer,
        db.ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    description = db.Column(
        db.Text,
        nullable=False,
    )

    valid_from = db.Column(
        db.Date,
        nullable=True,
    )

    valid_to = db.Column(
        db.Date,
        nullable=True,
    )

    company = db.relationship(
        "Company",
        back_populates="activities",
    )


class CompanyContact(db.Model):
    __tablename__ = "company_contacts"

    id = db.Column(db.Integer, primary_key=True)

    company_id = db.Column(
        db.Integer,
        db.ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    contact_type=db.Column(
        db.String(30),
        nullable=False,
        index=True
    )
    value=db.Column(
        db.String(500),
        nullable=False,
    )
    label=db.Column(
        db.String(100),
        nullable=True,
    )
    source_type=db.Column(
        db.String(50),
        nullable=False,
    )
    source_url=db.Column(
        db.Text,
        nullable=True,
    )


    is_verified = db.Column(
        db.Boolean,
        nullable=False,
        default=False,
    )

    is_primary = db.Column(
        db.Boolean,
        nullable=False,
        default=False,
    )

    last_verified_at = db.Column(
        db.DateTime,
        nullable=True,
    )

    created_at = db.Column(
        db.DateTime,
        nullable=False,
        default=utcnow,
    )

    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
    )

    company = db.relationship(
        "Company",
        back_populates="contacts",
    )
    confidence_score = db.Column(
        db.Float,
        nullable=True,
    )


class Campaign(db.Model):
    __tablename__ = "campaigns"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    offer_type = db.Column(db.String(30), nullable=False, default="service")
    offer_description = db.Column(db.Text, nullable=False)
    subject_template = db.Column(db.String(255), nullable=False)
    body_template = db.Column(db.Text, nullable=False)
    target_filters = db.Column(db.JSON, nullable=True)
    status = db.Column(
        db.String(30),
        nullable=False,
        default="draft",
        index=True,
    )
    daily_limit = db.Column(db.Integer, nullable=False, default=20)
    automation_enabled = db.Column(db.Boolean, nullable=False, default=False)
    offer_stage = db.Column(db.String(20), nullable=False, default="ready")
    targeting_profile = db.Column(db.JSON, nullable=True)
    target_total = db.Column(db.Integer, nullable=False, default=50)
    batch_size = db.Column(db.Integer, nullable=False, default=10)
    follow_up_days = db.Column(db.Integer, nullable=False, default=7)
    contact_cooldown_days = db.Column(db.Integer, nullable=False, default=90)
    activated_at = db.Column(db.DateTime(timezone=True), nullable=True)
    last_automation_run_at = db.Column(db.DateTime(timezone=True), nullable=True)
    last_automation_error = db.Column(db.Text, nullable=True)
    last_run_summary = db.Column(db.JSON, nullable=True)
    delivery_lock_token = db.Column(db.String(64), nullable=True)
    delivery_locked_at = db.Column(db.DateTime(timezone=True), nullable=True)
    completed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utcnow,
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
    )

    recipients = db.relationship(
        "CampaignRecipient",
        back_populates="campaign",
        cascade="all, delete-orphan",
        order_by="CampaignRecipient.created_at",
    )
    landing_page = db.relationship(
        "LandingPage",
        back_populates="campaign",
        cascade="all, delete-orphan",
        uselist=False,
    )


class LandingPage(db.Model):
    __tablename__ = "landing_pages"
    __table_args__ = (
        UniqueConstraint("campaign_id", name="uq_landing_page_campaign"),
        UniqueConstraint("slug", name="uq_landing_page_slug"),
        UniqueConstraint("preview_token", name="uq_landing_page_preview_token"),
    )

    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(
        db.Integer,
        db.ForeignKey("campaigns.id", ondelete="CASCADE"),
        nullable=False,
    )
    slug = db.Column(db.String(120), nullable=False)
    preview_token = db.Column(db.String(64), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="draft")
    content = db.Column(db.JSON, nullable=False)
    hero_image_url = db.Column(db.String(1000), nullable=True)
    hero_image_alt = db.Column(db.String(255), nullable=True)
    contact_email = db.Column(db.String(255), nullable=True)
    published_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utcnow,
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
    )

    campaign = db.relationship("Campaign", back_populates="landing_page")


class CampaignRecipient(db.Model):
    __tablename__ = "campaign_recipients"
    __table_args__ = (
        UniqueConstraint(
            "campaign_id",
            "company_id",
            name="uq_campaign_recipient_company",
        ),
        Index(
            "ix_campaign_recipient_campaign_attempt",
            "campaign_id",
            "sending_started_at",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(
        db.Integer,
        db.ForeignKey("campaigns.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    company_id = db.Column(
        db.Integer,
        db.ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    contact_id = db.Column(
        db.Integer,
        db.ForeignKey("company_contacts.id", ondelete="SET NULL"),
        nullable=True,
    )
    recipient_email = db.Column(db.String(255), nullable=False)
    subject = db.Column(db.String(255), nullable=False)
    body = db.Column(db.Text, nullable=False)
    fit_score = db.Column(db.Integer, nullable=True)
    fit_reason = db.Column(db.Text, nullable=True)
    selection_source = db.Column(db.String(30), nullable=True)
    status = db.Column(
        db.String(30),
        nullable=False,
        default="draft",
        index=True,
    )
    last_error = db.Column(db.Text, nullable=True)
    approved_at = db.Column(db.DateTime(timezone=True), nullable=True)
    sending_started_at = db.Column(db.DateTime(timezone=True), nullable=True)
    sent_at = db.Column(db.DateTime(timezone=True), nullable=True)
    replied_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utcnow,
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
    )

    campaign = db.relationship("Campaign", back_populates="recipients")
    company = db.relationship("Company", back_populates="campaign_recipients")
    contact = db.relationship("CompanyContact")
    outbound_emails = db.relationship(
        "OutboundEmail",
        back_populates="campaign_recipient",
    )
    email_replies = db.relationship(
        "EmailReply",
        back_populates="campaign_recipient",
    )


class Suppression(db.Model):
    __tablename__ = "suppressions"
    __table_args__ = (
        UniqueConstraint("scope", "value", name="uq_suppression_scope_value"),
    )

    id = db.Column(db.Integer, primary_key=True)
    scope = db.Column(db.String(20), nullable=False, index=True)
    value = db.Column(db.String(255), nullable=False)
    reason = db.Column(db.String(500), nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utcnow,
    )


class PostalLocation(db.Model):
    __tablename__ = "postal_locations"
    __table_args__ = (
        UniqueConstraint(
            "postal_code",
            "place_name",
            name="uq_postal_location_place",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    postal_code = db.Column(db.String(10), nullable=False, index=True)
    place_name = db.Column(db.String(255), nullable=False)
    search_name = db.Column(db.String(255), nullable=False, index=True)
    admin_name_1 = db.Column(db.String(255), nullable=True)
    admin_name_2 = db.Column(db.String(255), nullable=True)
    latitude = db.Column(db.Float, nullable=False)
    longitude = db.Column(db.Float, nullable=False)
    accuracy = db.Column(db.Integer, nullable=True)
    source = db.Column(db.String(50), nullable=False, default="geonames")
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utcnow,
    )
