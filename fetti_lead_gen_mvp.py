import streamlit as st
import pandas as pd
from datetime import datetime
import os

try:
    import openai
except ImportError:
    openai = None

import smtplib
from email.mime.text import MIMEText

DEFAULT_STATES = ["CA", "TX", "FL", "AZ", "NV", "CO", "GA"]
PRODUCT_TYPES = ["Any", "Refi", "Purchase", "DSCR Investor", "Bridge / Fix & Flip"]

COLUMN_ALIASES = {
    "first_name": ["first_name", "first name", "fname", "givenname"],
    "last_name": ["last_name", "last name", "lname", "surname"],
    "email": ["email", "email_address", "e-mail"],
    "phone": ["phone", "phone_number", "mobile"],
    "property_value": ["property_value", "est_value", "home_value", "zestimate"],
    "loan_purpose": ["purpose", "loan_purpose", "campaign", "deal_type"],
    "occupancy": ["occupancy", "occ_type", "owner_type"],
    "state": ["state", "property_state", "mail_state"],
    "credit_score": ["credit_score", "fico", "credit_band"],
    "liquid_assets": ["liquid_assets", "assets", "cash_reserves"],
    "cltv": ["cltv", "ltv", "combined_ltv"],
    "source": ["source", "lead_source", "origin"],
}


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    col_map = {}
    lower_map = {c.lower(): c for c in df.columns}
    for target, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in lower_map:
                col_map[lower_map[alias]] = target
                break
    df = df.rename(columns=col_map)
    return df


def safe_float(x, default=None):
    try:
        return float(x)
    except Exception:
        return default


def safe_str(x):
    if pd.isna(x):
        return ""
    return str(x).strip().lower()


def score_lead(row, config):
    score = 0

    purpose = safe_str(row.get("loan_purpose", ""))
    occ = safe_str(row.get("occupancy", ""))
    state = (row.get("state") or "").upper()

    desired_product = config["product_focus"]

    is_refi = any(k in purpose for k in ["refi", "refinance", "rate and term", "cash out", "cash-out"])
    is_purchase = "purchase" in purpose
    is_investor = any(k in occ for k in ["investor", "non-owner", "rental"])
    is_bridge = any(k in purpose for k in ["bridge", "fix and flip", "flip", "rehab"])

    if desired_product == "Refi" and not is_refi:
        return -1
    if desired_product == "Purchase" and not is_purchase:
        return -1
    if desired_product == "DSCR Investor" and not is_investor:
        return -1
    if desired_product == "Bridge / Fix & Flip" and not is_bridge:
        return -1

    score += 10

    if config["states"] and state in config["states"]:
        score += 10
    else:
        score -= 5

    val = safe_float(row.get("property_value"), None)
    if val:
        if 250_000 <= val <= 900_000:
            score += 15
        elif 900_000 < val <= 2_000_000:
            score += 12
        elif 150_000 <= val < 250_000:
            score += 5
        elif val > 2_000_000:
            score += 8

    cs = safe_float(row.get("credit_score"), None)
    min_cs = config["min_credit"]
    if cs is not None:
        if cs < min_cs:
            return -1
        if cs >= 740:
            score += 15
        elif cs >= 700:
            score += 10
        elif cs >= min_cs:
            score += 5

    assets = safe_float(row.get("liquid_assets"), None)
    min_assets = config["min_assets"]
    if assets is not None:
        if assets < min_assets:
            return -1
        if assets >= 150_000:
            score += 15
        elif assets >= 75_000:
            score += 10
        elif assets >= min_assets:
            score += 5

    cltv = safe_float(row.get("cltv"), None)
    if cltv is not None:
        if cltv <= 60:
            score += 15
        elif cltv <= 75:
            score += 10
        elif cltv <= 85:
            score += 5

    source = safe_str(row.get("source", ""))
    if "facebook" in source or "meta" in source:
        score += 5
    if "google" in source:
        score += 5
    if "data_vendor" in source or "list" in source:
        score += 2

    return score


def build_ai_prompt(lead: dict) -> str:
    return f"""
You are an expert private lender and DSCR / bridge loan underwriter for Fetti Financial Services.

Analyze this mortgage lead and produce a concise but detailed summary.

Lead data:
- Name: {lead.get('first_name','')} {lead.get('last_name','')}
- Email: {lead.get('email','')}
- Phone: {lead.get('phone','')}
- State: {lead.get('state','')}
- Occupancy: {lead.get('occupancy','')}
- Property value: {lead.get('property_value','')}
- Loan purpose: {lead.get('loan_purpose','')}
- Credit band or score: {lead.get('credit_band') or lead.get('credit_score')}
- Liquid assets: {lead.get('liquid_assets','')}
- Notes: {lead.get('notes','')}

Return your answer in this structure (plain text, no JSON):

1) Product fit: (e.g. DSCR refi, bridge, fix & flip, full doc, bank statement, etc.)
2) Estimated max LTV / CLTV you would be comfortable with and why.
3) If investor/rental: estimated DSCR and whether it seems to qualify.
4) Recommended loan amount range.
5) Strength score from 0–100 and one-line justification.
6) Key risks / red flags.
7) Exact “Next steps” Ramon should take with this borrower.
8) One-line subject line Ramon could use for follow-up email.
    """.strip()


def analyze_lead_with_ai(lead: dict) -> str:
    api_key = None
    try:
        api_key = st.secrets.get("OPENAI_API_KEY")  # type: ignore[attr-defined]
    except Exception:
        api_key = None

    if not api_key or openai is None:
        return "AI summary unavailable (missing OPENAI_API_KEY or openai library)."

    try:
        openai.api_key = api_key
        prompt = build_ai_prompt(lead)
        resp = openai.ChatCompletion.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are an expert mortgage and private lending underwriter."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=600,
            temperature=0.2,
        )
        text = resp["choices"][0]["message"]["content"]
        return text.strip()
    except Exception as e:
        return f"AI summary error: {e}"


def send_email_notification(subject: str, body: str, to_email: str) -> None:
    try:
        secrets = st.secrets  # type: ignore[attr-defined]
        host = secrets.get("SMTP_HOST", "")
        port = int(secrets.get("SMTP_PORT", 587))
        user = secrets.get("SMTP_USER", "")
        password = secrets.get("SMTP_PASSWORD", "")
        from_email = secrets.get("FROM_EMAIL", to_email)
    except Exception:
        return

    if not host or not user or not password:
        return

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = from_email
    msg["To"] = to_email

    try:
        with smtplib.SMTP(host, port) as server:
            server.starttls()
            server.login(user, password)
            server.send_message(msg)
    except Exception as e:
        st.warning(f"Email notification failed: {e}")


def view_scoring_tab():
    st.subheader("📊 Upload & Score Leads (CSV)")

    with st.sidebar:
        st.header("⚙️ Scoring Settings")

        product_focus = st.selectbox("Target Product", PRODUCT_TYPES, index=0)
        states = st.multiselect("Target States", DEFAULT_STATES, default=DEFAULT_STATES)
        min_credit = st.slider("Minimum Credit Score", min_value=540, max_value=780, value=640, step=10)
        min_assets = st.number_input("Minimum Liquid Assets ($)", min_value=0, value=20000, step=5000)

        config = {
            "product_focus": product_focus,
            "states": states,
            "min_credit": min_credit,
            "min_assets": min_assets,
        }

        st.markdown("---")
        st.caption("Tune this to your lending box.")

    st.write("Upload any lead CSV from Facebook, Google, data vendors, or your CRM.")

    uploaded_file = st.file_uploader("Drag & drop or browse for a CSV file", type=["csv"], key="score_uploader")

    if not uploaded_file:
        st.info("Upload a CSV to begin.")
        return

    df_raw = pd.read_csv(uploaded_file)
    st.write(f"Raw file shape: {df_raw.shape[0]} rows x {df_raw.shape[1]} columns")

    df = normalize_columns(df_raw)

    st.subheader("Preview normalized data")
    st.write("Auto-detected important columns like name, email, property value, purpose, etc.")
    st.dataframe(df.head())

    required_for_output = ["first_name", "last_name", "email", "phone"]
    missing = [c for c in required_for_output if c not in df.columns]
    if missing:
        st.warning(
            f"The following important columns are missing after normalization: {missing}. "
            "You can still score, but export quality may be limited."
        )

    st.subheader("Apply Fetti scoring rules")

    if st.button("Run Lead Scoring"):
        scores = []
        for _, row in df.iterrows():
            s = score_lead(row, config)
            scores.append(s)

        df_scored = df.copy()
        df_scored["fetti_score"] = scores
        df_scored = df_scored[df_scored["fetti_score"] >= 0]

        if df_scored.empty:
            st.error("No leads matched your current filters / thresholds. Loosen the rules and try again.")
            return

        df_scored = df_scored.sort_values("fetti_score", ascending=False)

        st.success(f"Scored {len(df_scored)} leads. Showing top 50 below.")
        st.dataframe(df_scored.head(50))

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_name = f"fetti_scored_leads_{product_focus.replace(' ', '_').lower()}_{timestamp}.csv"

        csv_bytes = df_scored.to_csv(index=False).encode("utf-8")
        st.download_button(
            label="⬇️ Download full scored lead list as CSV",
            data=csv_bytes,
            file_name=out_name,
            mime="text/csv",
        )


def view_capture_tab():
    st.subheader("📥 Capture New Leads (Web Form)")

    st.write(
        "Use this form as a live lead capture page. Send this page link to clients or "
        "run ads directly to it. Every submission is saved into captured_leads.csv and can trigger AI + email."
    )

    with st.form("lead_capture_form"):
        col1, col2 = st.columns(2)
        with col1:
            first_name = st.text_input("First Name")
        with col2:
            last_name = st.text_input("Last Name")

        email = st.text_input("Email")
        phone = st.text_input("Phone")

        col3, col4 = st.columns(2)
        with col3:
            state = st.text_input("Property State (e.g., CA, TX)")
        with col4:
            occupancy = st.selectbox("Occupancy", ["Owner", "Investor", "Non-Owner", "Second Home"])

        property_value = st.number_input("Estimated Property Value ($)", min_value=0, step=5000)
        loan_purpose = st.selectbox(
            "Loan Purpose",
            ["Refi", "Cash Out Refi", "Purchase", "DSCR", "Bridge", "Fix and Flip"],
        )

        credit_band = st.selectbox(
            "Credit Profile (Rough)",
            ["<620", "620-659", "660-699", "700-739", "740+"],
        )

        liquid_assets = st.number_input("Approx. Liquid Assets ($)", min_value=0, step=5000)

        notes = st.text_area("Notes (optional)")

        submitted = st.form_submit_button("Submit Lead")

    if submitted:
        if not first_name or not last_name or not (email or phone):
            st.error("First name, last name, and at least one contact (email or phone) are required.")
        else:
            row = {
                "first_name": first_name,
                "last_name": last_name,
                "email": email,
                "phone": phone,
                "state": state.upper().strip() if state else "",
                "occupancy": occupancy,
                "property_value": property_value if property_value else None,
                "loan_purpose": loan_purpose,
                "credit_band": credit_band,
                "liquid_assets": liquid_assets if liquid_assets else None,
                "notes": notes,
                "source": "web_form",
                "created_at": datetime.now().isoformat(timespec="seconds"),
            }

            leads_path = "captured_leads.csv"
            try:
                existing = pd.read_csv(leads_path)
                df_out = pd.concat([existing, pd.DataFrame([row])], ignore_index=True)
            except FileNotFoundError:
                df_out = pd.DataFrame([row])

            df_out.to_csv(leads_path, index=False)

            ai_summary = analyze_lead_with_ai(row)

            st.success("✅ Lead captured and saved to captured_leads.csv")
            st.subheader("🧠 AI Loan Summary")
            st.text(ai_summary)

            try:
                notify_email = st.secrets.get("NOTIFY_EMAIL", "info@fettifi.com")  # type: ignore[attr-defined]
            except Exception:
                notify_email = "info@fettifi.com"

            subject = f"🔥 New Fetti Lead: {first_name} {last_name} – {loan_purpose}"
            body = f"""
New lead captured via Fetti Leads app.

Name: {first_name} {last_name}
Email: {email}
Phone: {phone}
State: {state}
Occupancy: {occupancy}
Property value: {property_value}
Loan purpose: {loan_purpose}
Credit profile: {credit_band}
Liquid assets: {liquid_assets}
Notes: {notes}

--- AI Loan Summary ---
{ai_summary}

This lead is also stored in captured_leads.csv in your app workspace.
"""
            send_email_notification(subject, body, notify_email)

    st.markdown("---")
    st.write("Latest captured leads preview (from captured_leads.csv):")
    try:
        captured = pd.read_csv("captured_leads.csv")
        st.dataframe(captured.tail(10))
    except FileNotFoundError:
        st.info("No leads captured yet. Once someone submits the form, they will appear here.")


def main():
    st.set_page_config(page_title="Fetti Leads – Capture, Score & AI Qualify", layout="wide")

    st.markdown(
        "<h1 style='color:#0b8f3c;'>🧠 Fetti Leads – Real Lead Capture, Scoring & AI Qualification</h1>",
        unsafe_allow_html=True,
    )
    st.write(
        "Capture real mortgage and investor leads, score uploaded lists, and let Fetti AI pre-underwrite every deal."
    )

    tab1, tab2 = st.tabs(["📥 Capture New Leads", "📊 Upload & Score Leads"])

    with tab1:
        view_capture_tab()

    with tab2:
        view_scoring_tab()


if __name__ == "__main__":
    main()
