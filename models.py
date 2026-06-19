from datetime import datetime
from extensions import db


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