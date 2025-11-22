import streamlit as st
import pandas as pd
from datetime import datetime

# ---------------- CONFIG ---------------- #

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
        "run ads directly to it. Every submission is saved into captured_leads.csv."
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
            st.success("✅ Lead captured and saved to captured_leads.csv")

    st.markdown("---")
    st.write("Latest captured leads preview (from captured_leads.csv):")
    try:
        captured = pd.read_csv("captured_leads.csv")
        st.dataframe(captured.tail(10))
    except FileNotFoundError:
        st.info("No leads captured yet. Once someone submits the form, they will appear here.")


def main():
    st.set_page_config(page_title="Fetti Leads – Capture & Score", layout="wide")

    st.title("🧠 Fetti Leads – Real Lead Capture & Scoring")
    st.write(
        "Use this app in two ways: (1) capture real leads directly via the form, "
        "and (2) upload external lead lists to score and prioritize them."
    )

    tab1, tab2 = st.tabs(["📥 Capture New Leads", "📊 Upload & Score Leads"])

    with tab1:
        view_capture_tab()

    with tab2:
        view_scoring_tab()


if __name__ == "__main__":
    main()
