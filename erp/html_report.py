"""
Generate a highly visual, interactive HTML report (Plotly + custom CSS/JS)
covering the whole ERP system: Lead-to-Sale pipeline, Sales workload, lead
quality, support tickets, SLA status, and a timeline of everything currently
in progress.

Run:
    venv\\Scripts\\python.exe -m erp.html_report

Output:
    erp_report.html - open it in any browser (double-click). Fully
    interactive: hover for details, zoom, pan, click legend items to filter.
"""
import datetime as dt

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from sqlalchemy import text

from erp.db import engine

PLOTLY_CDN = '<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>'

# ---------------------------------------------------------------------------
# Palette (kept in one place so every chart + the page chrome stay in sync)
# ---------------------------------------------------------------------------
INK = "#1c1d22"
INK_DIM = "#6b6f76"
BLUE = "#3452eb"
CORAL = "#ff5a36"
GREEN = "#12b886"
VIOLET = "#7c5cff"
AMBER = "#f2b705"
RED = "#e0393e"
COLORWAY = [BLUE, CORAL, GREEN, VIOLET, AMBER]


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
def load_df(query: str) -> pd.DataFrame:
    with engine.connect() as conn:
        return pd.read_sql(text(query), conn)


def load_all():
    leads = load_df("SELECT * FROM v_leads_summary ORDER BY created_at")
    tickets = load_df("SELECT * FROM v_tickets_summary ORDER BY created_at")

    funnel_counts = load_df("""
        SELECT
          (SELECT count(*) FROM leads) AS total_leads,
          (SELECT count(*) FROM lead_assignments WHERE is_current) AS assigned,
          (SELECT count(DISTINCT lead_id) FROM lead_ai_analysis) AS analyzed,
          (SELECT count(*) FROM lead_clickup_sync WHERE sync_status IN ('synced','mocked')) AS task_created,
          (SELECT count(DISTINCT lead_id) FROM lead_updates) AS has_update
    """).iloc[0]

    timeline = load_df("""
        SELECT
            'Lead' AS task_type,
            l.full_name AS task_name,
            rep.full_name AS employee,
            la.assigned_at AS start_time,
            COALESCE(l.sla_due_at, la.assigned_at + INTERVAL '1 day') AS end_time,
            COALESCE(l.sla_due_at IS NOT NULL AND now() > l.sla_due_at, FALSE) AS sla_breached
        FROM leads l
        JOIN lead_assignments la ON la.lead_id = l.id AND la.is_current
        JOIN users rep ON rep.id = la.sales_rep_id

        UNION ALL

        SELECT
            'Ticket' AS task_type,
            t.ticket_code AS task_name,
            ag.full_name AS employee,
            t.created_at AS start_time,
            COALESCE(t.sla_due_at, t.created_at + INTERVAL '1 day') AS end_time,
            COALESCE(t.sla_due_at IS NOT NULL AND now() > t.sla_due_at, FALSE) AS sla_breached
        FROM tickets t
        JOIN users ag ON ag.id = t.assignee_id
        WHERE t.status NOT IN ('resolved', 'closed')

        ORDER BY employee, start_time
    """)

    return leads, tickets, funnel_counts, timeline


# ---------------------------------------------------------------------------
# Shared chart styling
# ---------------------------------------------------------------------------
def _style(fig: go.Figure, height: int = 360) -> go.Figure:
    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font={"family": "'Public Sans', sans-serif", "color": INK, "size": 12.5},
        title={"font": {"family": "'Fraunces', serif", "size": 19, "color": INK}},
        margin={"l": 48, "r": 24, "t": 56, "b": 40},
        height=height,
        colorway=COLORWAY,
        hoverlabel={"bgcolor": "#1c1d22", "font": {"family": "'IBM Plex Mono', monospace", "size": 11.5}},
    )
    fig.update_xaxes(showgrid=False, zeroline=False, showline=True, linecolor="#e7e2d8",
                      ticks="outside", tickcolor="#e7e2d8", ticklen=6)
    fig.update_yaxes(showgrid=True, gridcolor="#f2efe8", gridwidth=1, zeroline=False, showline=False)
    return fig


def _round_bars(fig: go.Figure, radius: int = 10) -> go.Figure:
    """Soften bar-based traces: rounded tops, translucent fill, colored outline
    instead of a flat solid block."""
    for trace in fig.data:
        if trace.type not in ("bar", "histogram"):
            continue
        color = trace.marker.color
        if isinstance(color, str) and color.startswith("#"):
            r, g, b = int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)
            trace.marker.color = f"rgba({r},{g},{b},0.82)"
            trace.marker.line = {"color": color, "width": 1.5}
        trace.marker.cornerradius = radius
    return fig


def _empty_chart(message: str) -> go.Figure:
    fig = go.Figure()
    fig.add_annotation(text=message, showarrow=False, font={"size": 15, "color": INK_DIM})
    fig.update_layout(xaxis={"visible": False}, yaxis={"visible": False}, height=220)
    return _style(fig, height=220)


# ---------------------------------------------------------------------------
# Chart builders
# ---------------------------------------------------------------------------
def chart_funnel(funnel_counts) -> go.Figure:
    stages = ["Lead received", "Assigned to Sales", "Analyzed by AI", "ClickUp task created", "Follow-up logged"]
    values = [
        int(funnel_counts["total_leads"]), int(funnel_counts["assigned"]),
        int(funnel_counts["analyzed"]), int(funnel_counts["task_created"]),
        int(funnel_counts["has_update"]),
    ]
    fig = go.Figure(go.Funnel(
        y=stages, x=values, textinfo="value+percent initial",
        marker={
            "color": ["rgba(255,90,54,.85)", "rgba(255,90,54,.85)", "rgba(124,92,255,.85)",
                      "rgba(52,82,235,.85)", "rgba(18,184,134,.85)"],
            "line": {"color": [CORAL, CORAL, VIOLET, BLUE, GREEN], "width": 1.5},
        },
        connector={"line": {"color": "#f2efe8", "width": 3}},
    ))
    fig.update_layout(title="Lead-to-Sale pipeline")
    return _style(fig, height=380)


def chart_timeline(timeline: pd.DataFrame) -> go.Figure:
    if timeline.empty:
        return _empty_chart("No active task assigned to any employee")
    timeline = timeline.copy()
    timeline["start_time"] = pd.to_datetime(timeline["start_time"])
    timeline["end_time"] = pd.to_datetime(timeline["end_time"])
    timeline["sla_label"] = timeline["sla_breached"].map({True: "SLA breached", False: "On time"})
    fig = px.timeline(
        timeline, x_start="start_time", x_end="end_time", y="employee",
        color="sla_label", hover_name="task_name",
        color_discrete_map={"On time": GREEN, "SLA breached": RED},
        title="Tasks in progress, per employee",
    )
    fig.update_yaxes(autorange="reversed")
    fig = _round_bars(fig, radius=14)
    return _style(fig, height=340)


def chart_potential_distribution(leads: pd.DataFrame) -> go.Figure:
    df = leads.dropna(subset=["potential_score"])
    if df.empty:
        return _empty_chart("No AI-analyzed lead yet")
    fig = px.histogram(df, x="potential_score", nbins=10, color="scale_estimate",
                        title="Lead potential score distribution")
    fig.update_layout(bargap=0.2)
    fig = _round_bars(fig, radius=8)
    return _style(fig)


def chart_source_breakdown(leads: pd.DataFrame) -> go.Figure:
    if leads.empty:
        return _empty_chart("No lead yet")
    counts = leads["source"].fillna("unknown").value_counts().reset_index()
    counts.columns = ["source", "count"]
    fig = px.pie(counts, names="source", values="count", hole=0.62, title="Leads by source")
    pull = [0.06 if c == counts["count"].max() else 0 for c in counts["count"]]
    fig.update_traces(textinfo="percent+label", pull=pull, opacity=0.88,
                       marker={"line": {"color": "#fff", "width": 3}},
                       rotation=15)
    fig.add_annotation(text=f"<b>{len(leads)}</b><br>leads", showarrow=False,
                        font={"family": "'Fraunces', serif", "size": 20, "color": INK})
    return _style(fig)


def chart_sla_summary(leads: pd.DataFrame, tickets: pd.DataFrame) -> go.Figure:
    rows = []
    if not leads.empty:
        rows.append({"category": "Leads", "status": "On time", "count": int((~leads["sla_breached"]).sum())})
        rows.append({"category": "Leads", "status": "SLA breached", "count": int(leads["sla_breached"].sum())})
    if not tickets.empty:
        rows.append({"category": "Tickets", "status": "On time", "count": int((~tickets["sla_breached"]).sum())})
        rows.append({"category": "Tickets", "status": "SLA breached", "count": int(tickets["sla_breached"].sum())})
    if not rows:
        return _empty_chart("No data yet")
    df = pd.DataFrame(rows)
    fig = px.bar(df, x="category", y="count", color="status", barmode="group",
                 color_discrete_map={"On time": GREEN, "SLA breached": RED},
                 title="SLA status: leads vs. tickets")
    fig = _round_bars(fig, radius=10)
    return _style(fig)


def chart_ticket_status(tickets: pd.DataFrame) -> go.Figure:
    if tickets.empty:
        return _empty_chart("No support ticket in the system right now")
    counts = tickets["status"].value_counts().reset_index()
    counts.columns = ["status", "count"]
    fig = px.bar(counts, x="status", y="count", color="status", title="Support tickets by status")
    fig.update_layout(showlegend=False)
    fig = _round_bars(fig, radius=10)
    return _style(fig)


def chart_leads_over_time(leads: pd.DataFrame) -> go.Figure:
    if leads.empty:
        return _empty_chart("No lead yet")
    df = leads.copy()
    df["created_at"] = pd.to_datetime(df["created_at"]).dt.date
    counts = df.groupby("created_at").size().reset_index(name="new_leads")
    fig = px.area(counts, x="created_at", y="new_leads", markers=True, title="New leads over time")
    fig.update_traces(line={"shape": "spline", "color": BLUE, "width": 3},
                       fillcolor="rgba(52,82,235,0.12)", marker={"size": 8, "color": BLUE})
    try:
        fig.update_traces(fillgradient={
            "type": "vertical",
            "colorscale": [[0, "rgba(52,82,235,0.28)"], [1, "rgba(52,82,235,0.0)"]],
        })
    except Exception:
        pass  # older Plotly.js without fillgradient support - the flat fillcolor above still applies
    return _style(fig)


def build_workload_dots(leads: pd.DataFrame) -> str:
    """A hand-built dot-grid (one dot per lead, colored by SLA status) - an
    editorial alternative to a plain bar chart."""
    if leads.empty:
        return '<div class="empty-note">No lead assigned yet</div>'

    blocks = []
    for rep, group in leads.groupby("assigned_sales_rep"):
        dots = "".join(
            f'<span class="dot {"dot-breach" if breached else "dot-ok"}" title="{name}"></span>'
            for name, breached in zip(group["full_name"], group["sla_breached"])
        )
        blocks.append(f"""
        <div class="dot-row">
          <div class="dot-row-head">
            <span class="dot-row-name">{rep}</span>
            <span class="dot-row-count">{len(group)}</span>
          </div>
          <div class="dot-grid">{dots}</div>
        </div>""")
    return "\n".join(blocks)


# ---------------------------------------------------------------------------
# Narrative insight (computed from the data, not fabricated)
# ---------------------------------------------------------------------------
def build_insight(leads: pd.DataFrame) -> tuple[str, str]:
    if leads.empty:
        return "The pipeline is empty.", "No lead has come in yet - once one does, this page will track it end to end."

    breach_pct = leads["sla_breached"].mean() * 100
    breached_count = int(leads["sla_breached"].sum())
    top_rep = leads["assigned_sales_rep"].value_counts().idxmax()
    top_count = int(leads["assigned_sales_rep"].value_counts().max())

    if breach_pct >= 99.5:
        headline = "Every active lead has breached its SLA."
    elif breach_pct == 0:
        headline = "Zero SLA breaches. The pipeline is healthy."
    else:
        headline = f"{breach_pct:.0f}% of active leads have breached SLA."

    sub = f"{breached_count} of {len(leads)} lead(s) are overdue right now. {top_rep} carries the heaviest load with {top_count} active lead(s)."
    return headline, sub


# ---------------------------------------------------------------------------
# Page assembly
# ---------------------------------------------------------------------------
def build_kpi_tiles(leads: pd.DataFrame, tickets: pd.DataFrame, funnel_counts) -> str:
    avg_score = leads["potential_score"].dropna().mean() if not leads.empty else None
    breach_pct = leads["sla_breached"].mean() * 100 if not leads.empty else 0

    tiles = [
        ("Total leads", len(leads), "0", "in the pipeline right now"),
        ("Avg. potential score", round(avg_score, 1) if pd.notna(avg_score) else 0, "1" if pd.notna(avg_score) else "0", "AI-scored 0-100"),
        ("SLA breach rate", round(breach_pct, 0), "0", "of active leads overdue"),
        ("ClickUp tasks", int(funnel_counts["task_created"]), "0", "created from leads"),
        ("Support tickets", len(tickets), "0", "open across departments"),
    ]
    html = []
    for i, (label, value, decimals, caption) in enumerate(tiles):
        suffix = "%" if label == "SLA breach rate" else ""
        html.append(f"""
        <div class="kpi" style="animation-delay:{i * 70}ms">
          <div class="kpi-value" data-count-to="{value}" data-decimals="{decimals}" data-suffix="{suffix}">0</div>
          <div class="kpi-label">{label}</div>
          <div class="kpi-caption">{caption}</div>
        </div>""")
    return "".join(html)


def build_report(output_path: str = "erp_report.html") -> None:
    leads, tickets, funnel_counts, timeline = load_all()
    headline, sub = build_insight(leads)

    figures = {
        "funnel": chart_funnel(funnel_counts),
        "timeline": chart_timeline(timeline),
        "potential": chart_potential_distribution(leads),
        "source": chart_source_breakdown(leads),
        "sla": chart_sla_summary(leads, tickets),
        "tickets": chart_ticket_status(tickets),
        "trend": chart_leads_over_time(leads),
    }
    charts_html = {
        key: fig.to_html(full_html=False, include_plotlyjs=False, config={"displaylogo": False})
        for key, fig in figures.items()
    }

    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ERP Report - Customer Support / Lead-to-Sale</title>
{PLOTLY_CDN}
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,600;9..144,700&family=Public+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>
:root {{
  --bg: #f5f3ef;
  --surface: #ffffff;
  --ink: {INK};
  --ink-dim: {INK_DIM};
  --line: #e7e2d8;
  --blue: {BLUE};
  --coral: {CORAL};
  --green: {GREEN};
  --violet: {VIOLET};
  --amber: {AMBER};
  --red: {RED};
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  background: var(--bg);
  color: var(--ink);
  font-family: 'Public Sans', -apple-system, Segoe UI, sans-serif;
}}
.page {{ max-width: 1280px; margin: 0 auto; padding: 40px 28px 80px; }}

.masthead {{ display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; margin-bottom: 28px; }}
.brand {{ display: flex; align-items: center; gap: 10px; font-family: 'Fraunces', serif; font-weight: 600; font-size: 19px; }}
.brand-mark {{ width: 30px; height: 30px; border-radius: 9px; background: linear-gradient(135deg, var(--coral), var(--violet)); flex: none; }}
.meta {{ font-family: 'IBM Plex Mono', monospace; font-size: 11.5px; color: var(--ink-dim); }}

.hero {{ margin-bottom: 32px; max-width: 760px; }}
.hero-title {{ font-family: 'Fraunces', serif; font-size: clamp(28px, 4vw, 42px); font-weight: 700; margin: 0 0 10px; line-height: 1.12; text-wrap: balance; }}
.hero-sub {{ font-size: 15px; color: var(--ink-dim); margin: 0; line-height: 1.6; }}

.kpi-row {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 14px; margin-bottom: 24px; }}
.kpi {{
  background: var(--surface); border: 1px solid var(--line); border-radius: 16px;
  padding: 20px 20px 18px; opacity: 0; transform: translateY(14px);
  animation: rise .6s ease forwards; transition: transform .2s ease, box-shadow .2s ease;
}}
.kpi:hover {{ transform: translateY(-3px); box-shadow: 0 12px 28px rgba(28,29,34,.08); }}
.kpi-value {{ font-family: 'Fraunces', serif; font-size: 34px; font-weight: 700; font-variant-numeric: tabular-nums; }}
.kpi-label {{ font-size: 13px; font-weight: 600; margin-top: 4px; }}
.kpi-caption {{ font-size: 11.5px; color: var(--ink-dim); margin-top: 2px; }}

.insight-banner {{
  border-radius: 20px; padding: 28px 30px; margin-bottom: 28px; color: #fff;
  background: linear-gradient(120deg, var(--coral), #ff8a4c, var(--violet));
  background-size: 220% 220%; animation: gradientshift 10s ease infinite, rise .6s ease forwards;
  opacity: 0; transform: translateY(14px); animation-delay: 0s, 320ms;
  animation-fill-mode: forwards, forwards;
}}
.insight-eyebrow {{ font-family: 'IBM Plex Mono', monospace; font-size: 11px; letter-spacing: .1em; text-transform: uppercase; opacity: .85; }}
.insight-headline {{ font-family: 'Fraunces', serif; font-size: clamp(22px, 3vw, 30px); font-weight: 700; margin: 6px 0 8px; text-wrap: balance; }}
.insight-sub {{ font-size: 14px; opacity: .95; max-width: 620px; line-height: 1.55; }}

.grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(420px, 1fr)); gap: 18px; }}
.card {{
  background: var(--surface); border: 1px solid var(--line); border-radius: 18px; padding: 6px 10px 10px;
  opacity: 0; transform: translateY(14px); animation: rise .6s ease forwards; transition: box-shadow .2s ease;
}}
.card:hover {{ box-shadow: 0 14px 32px rgba(28,29,34,.07); }}
.card-wide {{ grid-column: 1 / -1; }}
.card-title {{ font-family: 'Fraunces', serif; font-weight: 600; font-size: 16px; padding: 14px 14px 0; }}

.dot-row {{ padding: 10px 14px 16px; }}
.dot-row-head {{ display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 8px; }}
.dot-row-name {{ font-weight: 600; font-size: 14px; }}
.dot-row-count {{ font-family: 'IBM Plex Mono', monospace; color: var(--ink-dim); font-size: 12px; }}
.dot-grid {{ display: flex; flex-wrap: wrap; gap: 6px; }}
.dot {{ width: 13px; height: 13px; border-radius: 4px; transition: transform .15s ease; }}
.dot:hover {{ transform: scale(1.35); }}
.dot-ok {{ background: var(--green); }}
.dot-breach {{ background: var(--red); }}
.empty-note {{ padding: 30px; text-align: center; color: var(--ink-dim); font-size: 13px; }}

.chart-reveal {{ opacity: 0; transform: translateY(22px) scale(.98); transition: opacity .8s cubic-bezier(.16,1,.3,1), transform .8s cubic-bezier(.16,1,.3,1); }}
.chart-reveal.in-view {{ opacity: 1; transform: translateY(0) scale(1); }}

footer {{ margin-top: 36px; font-family: 'IBM Plex Mono', monospace; font-size: 11.5px; color: var(--ink-dim); text-align: center; }}

@keyframes rise {{ to {{ opacity: 1; transform: translateY(0); }} }}
@keyframes gradientshift {{ 0%,100% {{ background-position: 0% 50%; }} 50% {{ background-position: 100% 50%; }} }}
@media (prefers-reduced-motion: reduce) {{
  .kpi, .card, .insight-banner {{ animation: none !important; opacity: 1 !important; transform: none !important; }}
  .chart-reveal {{ transition: none !important; opacity: 1 !important; transform: none !important; }}
}}
.grid .card:nth-child(1) {{ animation-delay: 120ms; }}
.grid .card:nth-child(2) {{ animation-delay: 180ms; }}
.grid .card:nth-child(3) {{ animation-delay: 240ms; }}
.grid .card:nth-child(4) {{ animation-delay: 300ms; }}
.grid .card:nth-child(5) {{ animation-delay: 360ms; }}
.grid .card:nth-child(6) {{ animation-delay: 420ms; }}
.grid .card:nth-child(7) {{ animation-delay: 480ms; }}
</style>
</head>
<body>
  <div class="page">
    <div class="masthead">
      <div class="brand"><span class="brand-mark"></span>ERP Intelligence</div>
      <div class="meta">GENERATED {dt.datetime.now().strftime("%Y-%m-%d %H:%M")} &middot; LIVE SNAPSHOT FROM erp_support</div>
    </div>

    <div class="hero">
      <h1 class="hero-title">Customer Support &amp; Lead-to-Sale, at a glance</h1>
      <p class="hero-sub">A live read of the pipeline: where every lead and ticket stands, who's carrying the load, and what needs attention today.</p>
    </div>

    <div class="kpi-row">
      {build_kpi_tiles(leads, tickets, funnel_counts)}
    </div>

    <div class="insight-banner">
      <div class="insight-eyebrow">Today's read</div>
      <div class="insight-headline">{headline}</div>
      <div class="insight-sub">{sub}</div>
    </div>

    <div class="grid">
      <div class="card card-wide"><div class="chart-reveal">{charts_html['funnel']}</div></div>
      <div class="card card-wide"><div class="chart-reveal">{charts_html['timeline']}</div></div>
      <div class="card">
        <div class="card-title">Lead workload per Sales rep</div>
        <div class="chart-reveal">{build_workload_dots(leads)}</div>
      </div>
      <div class="card"><div class="chart-reveal">{charts_html['potential']}</div></div>
      <div class="card"><div class="chart-reveal">{charts_html['source']}</div></div>
      <div class="card"><div class="chart-reveal">{charts_html['sla']}</div></div>
      <div class="card"><div class="chart-reveal">{charts_html['tickets']}</div></div>
      <div class="card card-wide"><div class="chart-reveal">{charts_html['trend']}</div></div>
    </div>

    <footer>erp_support &middot; PostgreSQL 18 &middot; generated by erp/html_report.py</footer>
  </div>

<script>
document.querySelectorAll('[data-count-to]').forEach(function (el) {{
  var target = parseFloat(el.getAttribute('data-count-to'));
  var decimals = parseInt(el.getAttribute('data-decimals') || '0', 10);
  var suffix = el.getAttribute('data-suffix') || '';
  var duration = 900;
  var start = null;
  function step(ts) {{
    if (!start) start = ts;
    var progress = Math.min((ts - start) / duration, 1);
    var eased = 1 - Math.pow(1 - progress, 3);
    el.textContent = (target * eased).toFixed(decimals) + suffix;
    if (progress < 1) requestAnimationFrame(step);
  }}
  requestAnimationFrame(step);
}});

var revealObserver = new IntersectionObserver(function (entries) {{
  entries.forEach(function (entry) {{
    if (entry.isIntersecting) {{
      entry.target.classList.add('in-view');
      revealObserver.unobserve(entry.target);
      window.dispatchEvent(new Event('resize'));  // nudge Plotly to redraw at full size
    }}
  }});
}}, {{ threshold: 0.15 }});
document.querySelectorAll('.chart-reveal').forEach(function (el) {{ revealObserver.observe(el); }});
</script>
</body>
</html>
"""

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"Report saved to: {output_path}")


if __name__ == "__main__":
    build_report()
