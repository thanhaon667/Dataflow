"""
Internal dashboard for the Customer Support (support ticket) + Lead-to-Sale modules.

Run:
    ../venv/Scripts/python.exe -m streamlit run dashboard/streamlit_app.py
"""
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))  # allow "import erp" from the project root

import pandas as pd
import streamlit as st
from sqlalchemy import text

from erp import clickup_client
from erp.db import engine

st.set_page_config(page_title="ERP Customer Support Dashboard", layout="wide")
st.title("Customer Support / Lead-to-Sale Dashboard")


@st.cache_data(ttl=30)
def load_df(query: str) -> pd.DataFrame:
    with engine.connect() as conn:
        return pd.read_sql(text(query), conn)


tab_flow, tab_tickets, tab_leads, tab_staff = st.tabs(
    ["Pipeline", "Support Tickets", "Lead & Sale", "Staff & ClickUp"]
)

# ---------------------------------------------------------------------------
# Tab 0: Pipeline (Lead-to-Sale) - visualize each stage with live data
# ---------------------------------------------------------------------------
with tab_flow:
    st.subheader("Lead-to-Sale Pipeline")

    counts = load_df("""
        SELECT
          (SELECT count(*) FROM leads) AS total_leads,
          (SELECT count(*) FROM lead_assignments WHERE is_current) AS assigned,
          (SELECT count(DISTINCT lead_id) FROM lead_ai_analysis) AS analyzed,
          (SELECT count(*) FROM lead_clickup_sync WHERE sync_status IN ('synced','mocked')) AS task_created,
          (SELECT count(*) FROM lead_clickup_sync WHERE sync_status = 'synced') AS task_synced,
          (SELECT count(DISTINCT lead_id) FROM lead_updates) AS has_update,
          (SELECT count(*) FROM v_leads_summary WHERE sla_breached) AS sla_breach
    """).iloc[0]

    stages = [
        ("1. Lead received via webhook", int(counts["total_leads"]), "#c07a2e"),
        ("2. Assigned to a Sales rep", int(counts["assigned"]), "#c07a2e"),
        ("3. Analyzed by AI", int(counts["analyzed"]), "#7a6bb0"),
        ("4. ClickUp task created", int(counts["task_created"]), "#2f8f7a"),
        ("5. Sales rep has followed up", int(counts["has_update"]), "#5b6b8c"),
    ]
    max_count = max((s[1] for s in stages), default=0) or 1

    cols = st.columns(len(stages))
    for col, (label, value, color) in zip(cols, stages):
        pct = value / max_count
        col.markdown(
            f"""
            <div style="text-align:center">
              <div style="font-size:30px;font-weight:700;line-height:1.1">{value}</div>
              <div style="font-size:12px;opacity:0.65;margin:4px 0 8px;min-height:32px">{label}</div>
              <div style="background:rgba(128,128,128,0.25);border-radius:6px;height:10px;overflow:hidden">
                <div style="width:{pct * 100:.0f}%;background:{color};height:100%"></div>
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    st.caption(
        f"ClickUp tasks: {int(counts['task_synced'])} created for real, "
        f"{int(counts['task_created']) - int(counts['task_synced'])} still in mock mode "
        f"(CLICKUP_LIST_ID or token not configured yet). "
        f"SLA currently breached: {int(counts['sla_breach'])} lead(s)."
    )

    st.divider()
    st.markdown("##### Architecture flow (currently running)")

    FLOW_STEPS = [
        ("webhook", "Receive lead", "FastAPI /webhooks/leads", "rgba(192,122,46,0.12)"),
        ("db", "Dedup + assignment", "Duplicate email/phone -> existing rep. New -> least-loaded rep", "rgba(192,122,46,0.12)"),
        ("ai agent", "DeepSeek analysis", "Scale, potential, notes", "rgba(122,107,176,0.14)"),
        ("clickup api", "Create ClickUp task", "Assign rep, attach AI notes", "rgba(47,143,122,0.14)"),
        ("sales rep", "Rep follows up with lead", "Notes logged on the ClickUp task", "rgba(91,107,140,0.12)"),
        ("clickup_pull.py", "Sync back", "Pull comments into lead_updates", "rgba(47,143,122,0.14)"),
        ("weekly_report.py", "Weekly AI report", "Summary + suggested actions", "rgba(122,107,176,0.14)"),
    ]

    ARROW = '<div style="display:flex;align-items:center;opacity:0.4;font-size:18px;">&rarr;</div>'

    def _card(tag: str, name: str, desc: str, bg: str) -> str:
        return (
            f'<div style="flex:none;min-width:150px;padding:12px 14px;border-radius:10px;'
            f'background:{bg};border:1px solid rgba(128,128,128,0.25);">'
            f'<div style="font-size:10px;letter-spacing:.06em;opacity:0.6;text-transform:uppercase;">{tag}</div>'
            f'<div style="font-weight:700;font-size:13.5px;margin-top:2px;">{name}</div>'
            f'<div style="font-size:11.5px;opacity:0.7;margin-top:4px;">{desc}</div>'
            f'</div>'
        )

    cards_html = ARROW.join(_card(*step) for step in FLOW_STEPS)
    flow_html = (
        '<div style="display:flex;align-items:stretch;gap:6px;overflow-x:auto;padding:6px 2px 14px;">'
        + cards_html
        + "</div>"
    )
    st.markdown(flow_html, unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Tab 1: Support Tickets
# ---------------------------------------------------------------------------
with tab_tickets:
    df = load_df("SELECT * FROM v_tickets_summary ORDER BY created_at DESC")

    col1, col2, col3 = st.columns(3)
    col1.metric("Total tickets", len(df))
    col2.metric("Open / in progress", int(df["status"].isin(["open", "in_progress", "pending"]).sum()))
    col3.metric("SLA breached", int(df["sla_breached"].sum()) if not df.empty else 0)

    st.dataframe(df, use_container_width=True)

# ---------------------------------------------------------------------------
# Tab 2: Lead & Sale
# ---------------------------------------------------------------------------
with tab_leads:
    df = load_df("SELECT * FROM v_leads_summary ORDER BY created_at DESC")

    avg_score = df["potential_score"].dropna().mean() if not df.empty else None

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Total leads", len(df))
    col2.metric("Avg. potential score", round(avg_score, 1) if pd.notna(avg_score) else "-")
    col3.metric("Synced to ClickUp", int((df["sync_status"] == "synced").sum()) if not df.empty else 0)
    col4.metric("SLA breached (5 business hours)", int(df["sla_breached"].sum()) if not df.empty else 0)

    st.dataframe(df, use_container_width=True)

    if not df.empty:
        st.subheader("Lead workload by Sales rep (currently assigned)")
        workload = df.groupby("assigned_sales_rep").size().reset_index(name="lead_count")
        st.bar_chart(workload.set_index("assigned_sales_rep"))

# ---------------------------------------------------------------------------
# Tab 3: Staff & ClickUp cross-check
# ---------------------------------------------------------------------------
with tab_staff:
    st.subheader("Staff in the system (Database)")
    users_df = load_df("""
        SELECT u.id, u.full_name, u.email, d.name AS department, u.role, u.clickup_user_id
        FROM users u
        JOIN departments d ON d.id = u.department_id
        ORDER BY d.name, u.full_name
    """)

    try:
        clickup_members = clickup_client.get_team_members()
        clickup_error = None
    except Exception as e:
        clickup_members = []
        clickup_error = str(e)

    clickup_emails = {(m.get("email") or "").lower() for m in clickup_members}

    def check_status(row):
        if row["clickup_user_id"]:
            return "Mapped (has clickup_user_id)"
        if row["email"] and row["email"].lower() in clickup_emails:
            return "On ClickUp but NOT mapped in DB"
        return "Not on ClickUp yet"

    users_df["clickup_status"] = users_df.apply(check_status, axis=1)
    st.dataframe(users_df, use_container_width=True)

    st.subheader("Members currently in the ClickUp workspace")
    if clickup_error:
        st.error(f"Could not fetch ClickUp data: {clickup_error}")
    elif clickup_members:
        st.dataframe(pd.DataFrame(clickup_members), use_container_width=True)
        st.caption(
            "If a staff member isn't listed here yet: invite them to the ClickUp workspace first, "
            "then fill in clickup_user_id (the 'id' column above) on the users table in the Database."
        )
    else:
        st.info("Could not fetch the ClickUp member list (check CLICKUP_API_TOKEN in .env).")
