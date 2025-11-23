import os
from datetime import datetime

import pandas as pd
import streamlit as st
from openai import OpenAI
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ---------------------------
# CONFIG
# ---------------------------

st.set_page_config(
    page_title="Fetti Leads – Investment & Refi Lead Engine",
    layout="wide",
)

st.title("💸 Fetti Leads – Investment & Refi Capture")

# Read secrets
OPENAI_API_KEY = st.secrets.get("OPENAI_API_KEY", "")
SMTP_HOST = st.secrets.get("SMTP_HOST", "")
SMTP_PORT = int(st.secrets.get("SMTP_PORT", "587"))
SMTP_USER = st.secrets.get("SMTP_USER", "")
SMTP_PASSWORD = st.secrets.get("SMTP_PASSWORD", "")
FROM_EMAIL = st.secrets.get("FROM_EMAIL", SMTP_USER or "info@fettifi.com")
NOTIFY_EMAIL = st.secrets.get("NOTIFY_EMAIL", FROM_EMAIL)

# Initialize OpenAI client (new-style, no openai.ChatCompletion)
client = None
if OPENAI_API_KEY:
    client = OpenAI(api_key=OPENAI_API_KEY)


CAPTURED_LEADS_CSV = "captured_leads.csv"


# ---------------------------
# HELPER FUNCTIONS
# ---------------------------

def score_lead(row: dict) -> dict:
    """Simple scoring rules so you have a quick 'heat level' on each lead."""
    score = 0
    reasons = []

    # Credit band
    credit_band = str(row.get("credit_band", "")).strip()
    if credit_band in (">720", "720+", "680-720", "660-720"):
        score += 30
        reasons.append("Good credit profile.")
    elif credit_band in ("620-660",):
        score += 15
        reasons.append("Medium credit profile.")
    else:
        reasons.append("Sub-620 credit – higher risk.")

    # Equity
    try:
        property_value = float(row.get("property_value", 0) or 0)
        liquid_assets = float(row.get("liquid_assets", 0) or 0)
    except ValueError:
        property_value = 0
        liquid_assets = 0

    if property_value >= 500_000:
        score += 20
        reasons.append("Strong property value.")
    elif property_value >= 250_000:
        score += 10
        reasons.append("Decent property value.")

    if liquid_assets >= 100_000:
        score += 20
        reasons.append("High liquid assets.")
    elif liquid_assets >= 25_000:
        score += 10
        reasons.append("Some liquidity.")

    # Loan purpose
    loan_purpose = str(row.get("loan_purpose", "")).lower()
    if "refi" in loan_purpose:
        score += 10
        reasons.append("Refi opportunity.")
    if "dscr" in loan_purpose:
        score += 10
        reasons.append("Possible DSCR / investor loan.")

    # Simple banding
    if score >= 60:
        band = "🔥 HOT"
    elif score >= 40:
        band = "👍 WARM"
    else:
        band = "❄ COLD / LONG-TERM NURTURE"

    return {"score": score, "band": band, "reasons": "; ".join(reasons)}


def generate_ai_summary(lead: dict) -> str:
    """Use OpenAI (new client) to create an underwriter-style summary."""
    if client is None:
        return "AI summary error: OPENAI_API_KEY is not configured in Streamlit secrets."

    system_msg = (
        "You are a senior mortgage underwriter and loan strategist for "
        "investment and refinance properties. Your job is to quickly assess "
        "risk, highlight deal strengths/weaknesses, and recommend structure."
    )

    user_prompt = f"""
Analyze this mortgage lead and provide:

1. Quick profile snapshot
2. Key strengths
3. Key risks / red flags
4. Recommended loan strategy (product, LTV range, DSCR target if applicable)
5. Suggested maximum leverage (LTV %) and any cash-to-close notes
6. Call script angle for the loan officer's first phone call

Lead data (fields may be blank):

Name: {lead.get("first_name", "")} {lead.get("last_name", "")}
Email: {lead.get("email", "")}
Phone: {lead.get("phone", "")}
State: {lead.get("state", "")}
Occupancy: {lead.get("occupancy", "")}
Loan purpose: {lead.get("loan_purpose", "")}
Property value: {lead.get("property_value", "")}
Credit profile: {lead.get("credit_band", "")}
Liquid assets: {lead.get("liquid_assets", "")}
Notes: {lead.get("notes", "")}

Keep it under ~250 words. Use bullet points where helpful.
"""

    try:
        completion = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
        )
        return completion.choices[0].message.content.strip()
    except Exception as e:
        return f"AI summary error: {e}"


def send_email_notification(lead: dict, ai_summary: str, score_info: dict) -> str:
    """Send an email using your SMTP (Office365 via GoDaddy in your case)."""
    if not SMTP_HOST or not SMTP_USER or not SMTP_PASSWORD:
        return "Email not sent: SMTP settings are missing in Streamlit secrets."

    subject = f"🔥 New Fetti Lead: {lead.get('first_name','')} {lead.get('last_name','')}"

    # Build body
    body_lines = [
        "New lead captured via Fetti Leads app.\n",
        f"Name: {lead.get('first_name','')} {lead.get('last_name','')}",
        f"Email: {lead.get('email','')}",
        f"Phone: {lead.get('phone','')}",
        f"State: {lead.get('state','')}",
        f"Occupancy: {lead.get('occupancy','')}",
        f"Property value: {lead.get('property_value','')}",
        f"Loan purpose: {lead.get('loan_purpose','')}",
        f"Credit profile: {lead.get('credit_band','')}",
        f"Liquid assets: {lead.get('liquid_assets','')}",
        f"Notes: {lead.get('notes','')}",
        "",
        f"--- Lead Score ---",
        f"Score: {score_info.get('score')} | Band: {score_info.get('band')}",
        f"Reasons: {score_info.get('reasons')}",
        "",
        "--- AI Loan Summary ---",
        ai_summary,
        "",
        "This lead is also stored in captured_leads.csv in your app workspace.",
    ]
    body = "\n".join(body_lines)

    msg = MIMEMultipart()
    msg["From"] = FROM_EMAIL
    msg["To"] = NOTIFY_EMAIL
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)
        return "Email sent successfully."
    except Exception as e:
        return f"Email error: {e}"


def append_lead_to_csv(lead: dict, score_info: dict, ai_summary: str):
    """Persist the lead into a CSV inside the app workspace."""
    row = lead.copy()
    row["score"] = score_info.get("score")
    row["score_band"] = score_info.get("band")
    row["score_reasons"] = score_info.get("reasons")
    row["ai_summary"] = ai_summary
    row["created_at"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    df_new = pd.DataFrame([row])

    if os.path.exists(CAPTURED_LEADS_CSV):
        df_old = pd.read_csv(CAPTURED_LEADS_CSV)
        df_all = pd.concat([df_old, df_new], ignore_index=True)
    else:
        df_all = df_new

    df_all.to_csv(CAPTURED_LEADS_CSV, index=False)


def load_captured_leads() -> pd.DataFrame:
    if os.path.exists(CAPTURED_LEADS_CSV):
        return pd.read_csv(CAPTURED_LEADS_CSV)
    return pd.DataFrame()


# ---------------------------
# UI – TABS
# ---------------------------

tab1, tab2, tab3 = st.tabs(
    [
        "➕ Capture New Lead",
        "📥 Import & Score CSV",
        "📊 Captured Leads",
    ]
)

# ---------------------------
# TAB 1 – CAPTURE NEW LEAD
# ---------------------------
with tab1:
    st.subheader("Capture New Lead (Web form / Phone call)")

    with st.form("capture_lead_form"):
        col1, col2, col3 = st.columns(3)

        with col1:
            first_name = st.text_input("First name")
            last_name = st.text_input("Last name")
            email = st.text_input("Email")
            phone = st.text_input("Phone")

        with col2:
            state = st.text_input("State (2-letter)", value="CA")
            occupancy = st.selectbox(
                "Occupancy",
                ["Owner", "Investor", "Second home"],
                index=0,
            )
            loan_purpose = st.selectbox(
                "Loan purpose",
                ["Refi", "Purchase", "Cash-out Refi", "DSCR Refi", "DSCR Purchase"],
                index=0,
            )
            credit_band = st.selectbox(
                "Approx credit",
                ["<620", "620-660", "660-720", ">720"],
                index=1,
            )

        with col3:
            property_value = st.number_input(
                "Estimated property value",
                min_value=0.0,
                step=50000.0,
                value=500000.0,
                format="%.0f",
            )
            liquid_assets = st.number_input(
                "Liquid assets (cash, stocks, etc.)",
                min_value=0.0,
                step=5000.0,
                value=50000.0,
                format="%.0f",
            )
            notes = st.text_area("Notes (DTI, units, rents, story, etc.)", height=100)

        submitted = st.form_submit_button("Save lead + Run AI Summary")

    if submitted:
        lead = {
            "first_name": first_name.strip(),
            "last_name": last_name.strip(),
            "email": email.strip(),
            "phone": phone.strip(),
            "state": state.strip(),
            "occupancy": occupancy,
            "loan_purpose": loan_purpose,
            "credit_band": credit_band,
            "property_value": property_value,
            "liquid_assets": liquid_assets,
            "notes": notes.strip(),
        }

        score_info = score_lead(lead)
        ai_summary = generate_ai_summary(lead)

        append_lead_to_csv(lead, score_info, ai_summary)
        email_status = send_email_notification(lead, ai_summary, score_info)

        st.success("Lead captured.")
        st.write(f"**Score:** {score_info['score']} – {score_info['band']}")
        st.caption(score_info["reasons"])

        st.subheader("🧠 AI Loan Summary")
        st.write(ai_summary)

        st.info(email_status)


# ---------------------------
# TAB 2 – IMPORT & SCORE CSV
# ---------------------------
with tab2:
    st.subheader("Import CSV of existing leads and score them")

    uploaded_file = st.file_uploader("Upload CSV", type=["csv"])

    if uploaded_file is not None:
        df = pd.read_csv(uploaded_file)
        st.write("Preview of uploaded leads:")
        st.dataframe(df.head())

        if st.button("Score uploaded leads"):
            scores = []
            for _, row in df.iterrows():
                info = score_lead(row.to_dict())
                scores.append(info)

            df_scores = pd.DataFrame(scores)
            df_out = pd.concat([df.reset_index(drop=True), df_scores], axis=1)

            st.subheader("Scored leads")
            st.dataframe(df_out)

            out_name = "scored_leads.csv"
            df_out.to_csv(out_name, index=False)
            with open(out_name, "rb") as f:
                st.download_button(
                    label="⬇ Download scored_leads.csv",
                    data=f,
                    file_name=out_name,
                    mime="text/csv",
                )


# ---------------------------
# TAB 3 – CAPTURED LEADS
# ---------------------------
with tab3:
    st.subheader("Leads captured from this app")

    df_captured = load_captured_leads()
    if df_captured.empty:
        st.info("No leads captured yet.")
    else:
        st.dataframe(df_captured.sort_values("created_at", ascending=False))
