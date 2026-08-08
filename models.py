from datetime import datetime, timezone

from sqlalchemy import UniqueConstraint
from extensions import db

def utcnow() -> datetime:
    return datetime.now(timezone.utc)

class Lead(db.Model):
    id = db.Column(db.Integer, primary_key=True)

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

    from_email = db.Column(db.String(255), nullable=True)
    from_name = db.Column(db.String(255), nullable=True)
    subject = db.Column(db.String(255), nullable=True)

    text_body = db.Column(db.Text, nullable=True)
    html_body = db.Column(db.Text, nullable=True)

    postmark_message_id = db.Column(db.String(255), nullable=True)
    mailbox_hash = db.Column(db.String(255), nullable=True)
    ai_reply_draft = db.Column(db.Text, nullable=True)
    reply_sent_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    received_at = db.Column(db.DateTime, default=datetime.utcnow)

    lead = db.relationship("Lead", backref="email_replies")



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