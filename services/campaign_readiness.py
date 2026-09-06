"""Small shared preflight for UI activation and the actual delivery worker."""
from services.sender_profiles import profile_readiness


def campaign_delivery_issues(campaign):
    issues = []
    if (campaign.body_template or "").startswith("KONCEPT NA DOPRACOVANIE:"):
        issues.append("Koncept ponuky treba dopracovať pred aktiváciou alebo odoslaním.")
    targeting = campaign.targeting_profile or {}
    if campaign.automation_enabled and not (targeting.get("nace_keywords") or targeting.get("company_keywords")):
        issues.append("Pre AI výber doplň konkrétne SK NACE alebo firemné kľúčové slová.")
    if campaign.sender_profile_id is not None:
        if campaign.sender_profile is None:
            issues.append("Priradený profil odosielateľa neexistuje.")
        else:
            issues.extend(profile_readiness(campaign.sender_profile, require_imap=campaign.follow_up_enabled))
    if campaign.follow_up_enabled:
        if campaign.sender_profile is None:
            issues.append("Automatický follow-up vyžaduje vlastný profil odosielateľa.")
        if not campaign.follow_up_approved_at:
            issues.append("Text jedného follow-upu zatiaľ nie je výslovne schválený.")
        if not (campaign.follow_up_subject_template or "").strip() or not (campaign.follow_up_body_template or "").strip():
            issues.append("Chýba predmet alebo text follow-upu.")
    return issues
