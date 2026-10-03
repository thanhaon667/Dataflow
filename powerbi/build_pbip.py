"""
Builds the Power BI templates of ERP Desk as a Power BI Project (PBIP, text files):

  powerbi/ERPDesk.SemanticModel/   one shared model: 7 tables over the pbi_* views (db/sql/14_powerbi_views.sql), DirectQuery,
                                   relationships to a date table, ~35 DAX measures (TMDL)
  powerbi/Social.Report/           Social listening        (2 pages)
  powerbi/Leads.Report/            Leads and SLA           (2 pages)
  powerbi/Channels.Report/         Channels / marketing    (2 pages)
  powerbi/Social.pbip, Leads.pbip, Channels.pbip   one file per report; open any in Power BI Desktop
  powerbi/ERPDesk.theme.json       the ERP Desk look (palette, Montserrat, rounded white panels on a cream page)

  python powerbi/build_pbip.py           write everything
  python powerbi/build_pbip.py --check   also verify that every field a visual uses exists in the model (no file written when it fails)

The files are generated, not hand-edited: change this script and run it again. Report definitions use the PBIR folder format.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NS = uuid.UUID("5f0c9a7e-2b1d-4c63-9d0e-6a8f3b7c1e42")          # fixed namespace: the same input always gives the same GUIDs
BASE = "https://developer.microsoft.com/json-schemas/fabric/item"


def guid(name: str) -> str:
    return str(uuid.uuid5(NS, name))


# ============================================================================================ semantic model
# table -> {view, columns [(name, type, extra)], measures [(name, dax, format)]}
# type: s=string, i=int64, d=double, t=dateTime, b=boolean
MODEL = {
    "Calendar": {
        "view": "pbi_calendar", "date_table": True,
        "columns": [("date", "t", {"key": True, "format": "dd MMM yyyy"}), ("year", "i", {}), ("month_number", "i", {}),
                    ("month_name", "s", {"sort": "month_number"}), ("year_month", "s", {}), ("week_start", "t", {"format": "dd MMM yyyy"}),
                    ("day_name", "s", {"sort": "day_of_week"}), ("day_of_week", "i", {})],
        "measures": [],
    },
    "Mentions": {
        "view": "pbi_social_mention",
        "columns": [("mention_id", "i", {}), ("date", "t", {"format": "dd MMM yyyy"}), ("posted_at", "t", {}), ("hour_of_day", "i", {}), ("brand", "s", {}),
                    ("is_own_brand", "b", {}), ("platform", "s", {}), ("sentiment", "s", {}), ("sentiment_score", "i", {}), ("likes", "i", {}),
                    ("comments", "i", {}), ("shares", "i", {}), ("views", "i", {}), ("engagement", "i", {}), ("snippet", "s", {})],
        "measures": [
            ("Mentions", "COUNTROWS(Mentions)", "#,0"),
            ("Engagement", "SUM(Mentions[engagement])", "#,0"),
            ("Views", "SUM(Mentions[views])", "#,0"),
            ("Positive", 'CALCULATE([Mentions], Mentions[sentiment] = "positive")', "#,0"),
            ("Neutral", 'CALCULATE([Mentions], Mentions[sentiment] = "neutral")', "#,0"),
            ("Negative", 'CALCULATE([Mentions], Mentions[sentiment] = "negative")', "#,0"),
            ("Net Sentiment", "DIVIDE([Positive] - [Negative], [Mentions])", "+0.0%;-0.0%;0.0%"),
            ("Negative %", "DIVIDE([Negative], [Mentions])", "0.0%"),
            ("Positive %", "DIVIDE([Positive], [Mentions])", "0.0%"),
            ("Share of Voice", "DIVIDE([Mentions], CALCULATE([Mentions], REMOVEFILTERS(Mentions[brand])))", "0.0%"),
            ("Mentions Previous Month", "CALCULATE([Mentions], DATEADD('Calendar'[date], -1, MONTH))", "#,0"),
            ("Mentions vs Previous Month", "DIVIDE([Mentions] - [Mentions Previous Month], [Mentions Previous Month])", "+0.0%;-0.0%;0.0%"),
            ("Avg Mentions per Day", "AVERAGEX(VALUES('Calendar'[date]), [Mentions])", "#,0"),
            ("Engagement per Mention", "DIVIDE([Engagement], [Mentions])", "#,0.0"),
        ],
    },
    "MentionTopics": {
        "view": "pbi_social_mention_topic",
        "columns": [("mention_id", "i", {}), ("topic", "s", {}), ("theme", "s", {}), ("date", "t", {"format": "dd MMM yyyy"}), ("brand", "s", {}),
                    ("platform", "s", {}), ("sentiment", "s", {}), ("engagement", "i", {})],
        "measures": [
            ("Topic Mentions", "COUNTROWS(MentionTopics)", "#,0"),
            ("Topic Negative", 'CALCULATE([Topic Mentions], MentionTopics[sentiment] = "negative")', "#,0"),
            ("Topic Negative %", "DIVIDE([Topic Negative], [Topic Mentions])", "0%"),
        ],
    },
    "Events": {
        "view": "pbi_social_event",
        "columns": [("date", "t", {"format": "dd MMM yyyy"}), ("label", "s", {})],
        "measures": [],
    },
    "Batches": {
        "view": "pbi_social_batch",
        "columns": [("batch_id", "i", {}), ("source_file", "s", {}), ("loaded_at", "t", {}), ("rows_in", "i", {}), ("rows_bad", "i", {}),
                    ("rows_dup", "i", {}), ("rows_spam", "i", {}), ("rows_kept", "i", {})],
        "measures": [("Rows Loaded", "SUM(Batches[rows_in])", "#,0"), ("Rows Kept", "SUM(Batches[rows_kept])", "#,0"),
                     ("Rows Removed", "SUM(Batches[rows_bad]) + SUM(Batches[rows_dup]) + SUM(Batches[rows_spam])", "#,0")],
    },
    "Leads": {
        "view": "pbi_leads",
        "columns": [("lead_id", "i", {}), ("created_date", "t", {"format": "dd MMM yyyy"}), ("created_at", "t", {}), ("source", "s", {}), ("company", "s", {}),
                    ("sales_rep", "s", {}), ("assignment_reason", "s", {}), ("potential_score", "i", {}), ("organization_type", "s", {}),
                    ("scale_estimate", "s", {}), ("clickup_status", "s", {}), ("sla_due_at", "t", {}), ("first_reply_at", "t", {}),
                    ("reply_minutes", "d", {}), ("reply_bucket", "s", {}), ("sla_status", "s", {})],
        "measures": [
            ("Lead Count", "COUNTROWS(Leads)", "#,0"),
            ("Replied in Time", 'CALCULATE([Lead Count], Leads[sla_status] = "Replied in time")', "#,0"),
            ("Replied Late", 'CALCULATE([Lead Count], Leads[sla_status] = "Replied late")', "#,0"),
            ("Waiting Past SLA", 'CALCULATE([Lead Count], Leads[sla_status] = "Waiting, past SLA")', "#,0"),
            ("Waiting In Time", 'CALCULATE([Lead Count], Leads[sla_status] = "Waiting, in time")', "#,0"),
            ("SLA Met %", "DIVIDE([Replied in Time], [Replied in Time] + [Replied Late] + [Waiting Past SLA])", "0.0%"),
            ("Median Reply Minutes", "MEDIAN(Leads[reply_minutes])", "#,0"),
            ("Avg Potential Score", "AVERAGE(Leads[potential_score])", "0.0"),
            ("Leads Previous Month", "CALCULATE([Lead Count], DATEADD('Calendar'[date], -1, MONTH))", "#,0"),
            ("Leads vs Previous Month", "DIVIDE([Lead Count] - [Leads Previous Month], [Leads Previous Month])", "+0.0%;-0.0%;0.0%"),
        ],
    },
    "Channels": {
        "view": "pbi_channel_daily",
        "columns": [("date", "t", {"format": "dd MMM yyyy"}), ("channel", "s", {}), ("medium", "s", {}), ("paid_or_organic", "s", {}), ("currency", "s", {}),
                    ("grain", "s", {}), ("impressions", "i", {}), ("clicks", "i", {}), ("sessions", "i", {}), ("conversions", "i", {}),
                    ("spend", "d", {}), ("revenue", "d", {})],
        "measures": [
            ("Sessions", "SUM(Channels[sessions])", "#,0"),
            ("Clicks", "SUM(Channels[clicks])", "#,0"),
            ("Impressions", "SUM(Channels[impressions])", "#,0"),
            ("Conversions", "SUM(Channels[conversions])", "#,0"),
            ("CTR", "DIVIDE([Clicks], [Impressions])", "0.00%"),
            ("Conversion Rate", "DIVIDE([Conversions], [Clicks])", "0.00%"),
            # Money is never added across currencies: these show a value only while ONE currency is in the filter context.
            ("Spend", "IF(HASONEVALUE(Channels[currency]), SUM(Channels[spend]), BLANK())", "#,0.00"),
            ("Revenue", "IF(HASONEVALUE(Channels[currency]), SUM(Channels[revenue]), BLANK())", "#,0.00"),
            ("Cost per Conversion", "IF(HASONEVALUE(Channels[currency]), DIVIDE(SUM(Channels[spend]), [Conversions]), BLANK())", "#,0.00"),
            ("Currencies in View", "DISTINCTCOUNT(Channels[currency])", "0"),
            ("Sessions Previous Month", "CALCULATE([Sessions], DATEADD('Calendar'[date], -1, MONTH))", "#,0"),
            ("Sessions vs Previous Month", "DIVIDE([Sessions] - [Sessions Previous Month], [Sessions Previous Month])", "+0.0%;-0.0%;0.0%"),
        ],
    },
}
RELATIONSHIPS = [("Mentions", "date"), ("MentionTopics", "date"), ("Events", "date"), ("Leads", "created_date"), ("Channels", "date")]   # each -> Calendar[date]
TYPES = {"s": "string", "i": "int64", "d": "double", "t": "dateTime", "b": "boolean"}


def tmdl_name(n: str) -> str:
    return f"'{n}'" if not n.replace("_", "").isalnum() or n[0].isdigit() else n


def write_model(out: Path) -> None:
    d = out / "definition"
    (d / "tables").mkdir(parents=True, exist_ok=True)
    (out / "definition.pbism").write_text(json.dumps({"version": "4.0", "settings": {}}, indent=2) + "\n", encoding="utf-8")
    (d / "database.tmdl").write_text("database ERPDesk\n\tcompatibilityLevel: 1567\n", encoding="utf-8")
    refs = "\n".join(f"ref table {tmdl_name(t)}" for t in MODEL)
    (d / "model.tmdl").write_text(
        "model Model\n\tculture: en-US\n\tdefaultPowerBIDataSourceVersion: powerBI_V3\n\tsourceQueryCulture: en-US\n\tdiscourageImplicitMeasures\n\n" + refs + "\n\nref expression PgServer\nref expression PgDatabase\n",
        encoding="utf-8")
    (d / "expressions.tmdl").write_text(
        'expression PgServer = "127.0.0.1" meta [IsParameterQuery = true, Type = "Text", IsParameterQueryRequired = true]\n'
        '\tlineageTag: ' + guid("p-server") + '\n\n'
        'expression PgDatabase = "erp_support" meta [IsParameterQuery = true, Type = "Text", IsParameterQueryRequired = true]\n'
        '\tlineageTag: ' + guid("p-db") + '\n', encoding="utf-8")
    for t, spec in MODEL.items():
        L = [f"table {tmdl_name(t)}", f"\tlineageTag: {guid('t-' + t)}"]
        if spec.get("date_table"):
            L.append("\tdataCategory: Time")
        L.append("")
        for name, typ, extra in spec["columns"]:
            L += [f"\tcolumn {tmdl_name(name)}", f"\t\tdataType: {TYPES[typ]}"]
            if extra.get("format"):
                L.append(f"\t\tformatString: {extra['format']}")
            if extra.get("key"):
                L.append("\t\tisKey")
            L += [f"\t\tlineageTag: {guid('c-' + t + name)}", "\t\tsummarizeBy: none", f"\t\tsourceColumn: {name}"]
            if extra.get("sort"):
                L.append(f"\t\tsortByColumn: {extra['sort']}")
            L.append("")
        for name, dax, fmt in spec["measures"]:
            L += [f"\tmeasure {tmdl_name(name)} = {dax}", f"\t\tformatString: {fmt}", f"\t\tlineageTag: {guid('m-' + t + name)}", ""]
        L += [f"\tpartition {tmdl_name(t)} = m", "\t\tmode: directQuery", "\t\tsource =", "\t\t\t\tlet",
              "\t\t\t\t    Source = PostgreSQL.Database(PgServer, PgDatabase),",
              f"\t\t\t\t    Data = Source{{[Schema = \"public\", Item = \"{spec['view']}\"]}}[Data]", "\t\t\t\tin", "\t\t\t\t    Data", ""]
        (d / "tables" / f"{t}.tmdl").write_text("\n".join(L), encoding="utf-8")
    R = []
    for t, col in RELATIONSHIPS:
        R += [f"relationship {guid('r-' + t)}", f"\tfromColumn: {t}.{col}", "\ttoColumn: Calendar.date", ""]
    (d / "relationships.tmdl").write_text("\n".join(R), encoding="utf-8")


# ============================================================================================ report builder
def lit(v) -> dict:
    return {"expr": {"Literal": {"Value": v}}}


def txt(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def color(hexv: str) -> dict:
    return {"solid": {"color": lit(txt(hexv))}}


def col(t, c):
    return {"Column": {"Expression": {"SourceRef": {"Entity": t}}, "Property": c}}


def mea(t, m):
    return {"Measure": {"Expression": {"SourceRef": {"Entity": t}}, "Property": m}}


def proj(field, active=False):
    kind = "Column" if "Column" in field else "Measure"
    t, p = field[kind]["Expression"]["SourceRef"]["Entity"], field[kind]["Property"]
    out = {"field": field, "queryRef": f"{t}.{p}", "nativeQueryRef": p}
    if active:
        out["active"] = True
    return out


class Page:
    def __init__(self, name: str, title: str):
        self.name, self.title, self.visuals, self.n = name, title, [], 0

    def add(self, vtype, x, y, w, h, roles, title=None, objects=None, sort=None, container=None):
        self.n += 1
        q = {"queryState": {r: {"projections": [proj(f, active=(r in ("Category", "Rows", "Columns")) and i == 0) for i, f in enumerate(fs)]} for r, fs in roles.items()}} if roles else None
        if q and sort:
            q["sortDefinition"] = {"sort": [{"field": sort[0], "direction": sort[1]}], "isDefaultSort": True}
        vc = {"title": [{"properties": {"show": lit("true" if title else "false"), **({"text": lit(txt(title))} if title else {})}}]}
        vc.update(container or {})
        visual = {"visualType": vtype, "drillFilterOtherVisuals": True, "visualContainerObjects": vc}
        if q:
            visual["query"] = q
        if objects:
            visual["objects"] = objects
        self.visuals.append({"$schema": f"{BASE}/report/definition/visualContainer/1.0.0/schema.json", "name": f"{self.name}_v{self.n:02d}",
                             "position": {"x": x, "y": y, "z": self.n, "height": h, "width": w, "tabOrder": self.n}, "visual": visual})

    def heading(self, title, sub):
        """Page title and one-line description, as plain text boxes."""
        for i, (text, size, bold, y, h, col_) in enumerate([(title, 24, True, 8, 38, "#1c1d22"), (sub, 12, False, 46, 24, "#6b6f76")]):
            self.n += 1
            run = {"value": text, "textStyle": {"fontSize": f"{size}pt", **({"fontWeight": "bold"} if bold else {}), "color": col_}}
            self.visuals.append({"$schema": f"{BASE}/report/definition/visualContainer/1.0.0/schema.json", "name": f"{self.name}_h{i}",
                                 "position": {"x": 16, "y": y, "z": self.n, "height": h, "width": 1248, "tabOrder": self.n},
                                 "visual": {"visualType": "textbox", "drillFilterOtherVisuals": True,
                                            "objects": {"general": [{"properties": {"paragraphs": [{"textRuns": [run]}]}}]},
                                            "visualContainerObjects": {"background": [{"properties": {"show": lit("false")}}], "border": [{"properties": {"show": lit("false")}}],
                                                                       "title": [{"properties": {"show": lit("false")}}]}}})


def slicer(page, x, w, field, title, mode="Dropdown"):
    page.add("slicer", x, 76, w, 54, {"Values": [field]}, title=title,
             objects={"data": [{"properties": {"mode": lit(txt(mode))}}]})


def kpi(page, i, measure, title):
    page.add("card", 16 + i * 252, 138, 240, 84, {"Values": [measure]}, title=title)


def write_report(out: Path, pages: list[Page], theme_json: dict) -> None:
    d = out / "definition"
    if out.exists():
        shutil.rmtree(out)
    (d / "pages").mkdir(parents=True)
    (out / "StaticResources" / "RegisteredResources").mkdir(parents=True)
    (out / "StaticResources" / "RegisteredResources" / "ERPDesk.json").write_text(json.dumps(theme_json, indent=2), encoding="utf-8")
    (out / "definition.pbir").write_text(json.dumps({"$schema": f"{BASE}/report/definitionProperties/2.0.0/schema.json", "version": "4.0",
                                                     "datasetReference": {"byPath": {"path": "../ERPDesk.SemanticModel"}}}, indent=2) + "\n", encoding="utf-8")
    (d / "version.json").write_text(json.dumps({"$schema": f"{BASE}/report/definition/versionMetadata/1.0.0/schema.json", "version": "2.0.0"}, indent=2) + "\n", encoding="utf-8")
    (d / "report.json").write_text(json.dumps({
        "$schema": f"{BASE}/report/definition/report/1.0.0/schema.json",
        "themeCollection": {"customTheme": {"name": "ERPDesk.json", "reportVersionAtImport": "5.59", "type": "RegisteredResources"}},
        "resourcePackages": [{"name": "RegisteredResources", "type": "RegisteredResources", "items": [{"name": "ERPDesk.json", "path": "ERPDesk.json", "type": "CustomTheme"}]}],
        "settings": {"useStylableVisualContainerHeader": True, "exportDataMode": "AllowSummarized"}}, indent=2) + "\n", encoding="utf-8")
    (d / "pages" / "pages.json").write_text(json.dumps({"$schema": f"{BASE}/report/definition/pagesMetadata/1.0.0/schema.json",
                                                        "pageOrder": [p.name for p in pages], "activePageName": pages[0].name}, indent=2) + "\n", encoding="utf-8")
    for p in pages:
        pd = d / "pages" / p.name
        (pd / "visuals").mkdir(parents=True)
        (pd / "page.json").write_text(json.dumps({"$schema": f"{BASE}/report/definition/page/1.0.0/schema.json", "name": p.name, "displayName": p.title,
                                                  "displayOption": "FitToPage", "height": 720, "width": 1280}, indent=2) + "\n", encoding="utf-8")
        for v in p.visuals:
            vd = pd / "visuals" / v["name"]
            vd.mkdir()
            (vd / "visual.json").write_text(json.dumps(v, indent=2) + "\n", encoding="utf-8")


def theme() -> dict:
    ink, line = "#1c1d22", "#e7e2d8"
    def f(size, bold=False, c=ink):
        return {"fontFace": "Montserrat", "fontSize": size, "color": c, **({"fontWeight": "bold"} if bold else {})}
    return {
        "name": "ERP Desk", "dataColors": ["#3452eb", "#ff5a36", "#12b886", "#7c5cff", "#f2b705", "#e0393e", "#1c1d22", "#9a968b"],
        "good": "#12b886", "neutral": "#f2b705", "bad": "#e0393e", "maximum": "#3452eb", "center": "#f2b705", "minimum": "#dbe1fb",
        "foreground": ink, "foregroundNeutralSecondary": "#6b6f76", "backgroundLight": "#f5f3ef", "backgroundNeutral": line, "background": "#ffffff",
        "tableAccent": "#3452eb", "hyperlink": "#3452eb",
        "textClasses": {"callout": f(32, True), "title": f(13, True), "header": f(12, True), "label": f(10, False, "#6b6f76"), "largeTitle": f(24, True),
                        "smallLabel": f(9, False, "#6b6f76")},
        "visualStyles": {
            "*": {"*": {
                "title": [{"show": True, "fontFamily": "Montserrat", "fontSize": 12, "bold": True, "fontColor": {"solid": {"color": ink}}, "alignment": "left"}],
                "background": [{"show": True, "color": {"solid": {"color": "#ffffff"}}, "transparency": 0}],
                "border": [{"show": True, "color": {"solid": {"color": line}}, "radius": 16}],
                "dropShadow": [{"show": False}],
                "legend": [{"show": True, "position": "Top", "fontSize": 10}],
                "categoryAxis": [{"fontSize": 10, "labelColor": {"solid": {"color": "#6b6f76"}}}],
                "valueAxis": [{"fontSize": 10, "labelColor": {"solid": {"color": "#6b6f76"}}, "gridlineColor": {"solid": {"color": "#f2efe8"}}}]}},
            "page": {"*": {"background": [{"color": {"solid": {"color": "#f5f3ef"}}, "transparency": 0}],
                           "outspace": [{"color": {"solid": {"color": "#f5f3ef"}}}]}},
            "card": {"*": {"labels": [{"fontSize": 28, "fontFamily": "Montserrat", "bold": True}], "categoryLabels": [{"show": False}]}},
            "slicer": {"*": {"header": [{"show": False}], "items": [{"fontSize": 11}]}},
        }}


# ---------------------------------------------------------------------------------------------- the three reports
def social() -> list[Page]:
    p = Page("social_overview", "Overview")
    p.heading("Social listening", "Mentions, sentiment and share of voice of the brand against its competitors. Spam is already excluded.")
    slicer(p, 16, 420, col("Calendar", "date"), "Date range", "Between")
    slicer(p, 448, 280, col("Mentions", "brand"), "Brand")
    slicer(p, 740, 280, col("Mentions", "platform"), "Channel")
    for i, (m, t) in enumerate([("Mentions", "Mentions"), ("Engagement", "Engagement"), ("Net Sentiment", "Net sentiment"), ("Negative %", "Negative share"), ("Share of Voice", "Share of voice")]):
        kpi(p, i, mea("Mentions", m), t)
    p.add("lineChart", 16, 232, 760, 238, {"Category": [col("Calendar", "date")], "Y": [mea("Mentions", "Mentions")], "Series": [col("Mentions", "brand")]}, title="Mentions per day")
    p.add("hundredPercentStackedBarChart", 788, 232, 476, 238, {"Category": [col("Mentions", "brand")], "Y": [mea("Mentions", "Mentions")], "Series": [col("Mentions", "sentiment")]}, title="Sentiment mix per brand")
    p.add("clusteredBarChart", 16, 482, 620, 226, {"Category": [col("MentionTopics", "topic")], "Y": [mea("MentionTopics", "Topic Mentions")]}, title="Topics", sort=(mea("MentionTopics", "Topic Mentions"), "Descending"))
    p.add("clusteredColumnChart", 648, 482, 616, 226, {"Category": [col("Mentions", "platform")], "Y": [mea("Mentions", "Mentions")], "Series": [col("Mentions", "sentiment")]}, title="Channels")
    d = Page("social_detail", "Detail")
    d.heading("Social listening: detail", "Where complaints concentrate, when people post, the posts with most engagement and the data behind the numbers.")
    slicer(d, 16, 420, col("Calendar", "date"), "Date range", "Between")
    slicer(d, 448, 280, col("Mentions", "brand"), "Brand")
    slicer(d, 740, 280, col("Mentions", "sentiment"), "Sentiment")
    d.add("pivotTable", 16, 138, 620, 296, {"Rows": [col("MentionTopics", "topic")], "Columns": [col("MentionTopics", "brand")], "Values": [mea("MentionTopics", "Topic Negative %")]}, title="Negative share by topic and brand")
    d.add("clusteredColumnChart", 648, 138, 616, 296, {"Category": [col("Mentions", "hour_of_day")], "Y": [mea("Mentions", "Mentions")]}, title="Posts by hour of day")
    d.add("tableEx", 16, 446, 760, 262, {"Values": [col("Mentions", "date"), col("Mentions", "brand"), col("Mentions", "platform"), col("Mentions", "sentiment"), mea("Mentions", "Engagement"), col("Mentions", "snippet")]},
          title="Posts with most engagement", sort=(mea("Mentions", "Engagement"), "Descending"))
    d.add("tableEx", 788, 446, 476, 126, {"Values": [col("Events", "date"), col("Events", "label")]}, title="Events marked on the charts")
    d.add("tableEx", 788, 582, 476, 126, {"Values": [col("Batches", "source_file"), mea("Batches", "Rows Loaded"), mea("Batches", "Rows Removed"), mea("Batches", "Rows Kept")]}, title="Cleaning: rows in and out")
    return [p, d]


def leads() -> list[Page]:
    p = Page("leads_overview", "Overview")
    p.heading("Leads and SLA", "Where leads come from, who answers them and how many wait past their SLA.")
    slicer(p, 16, 420, col("Calendar", "date"), "Date range", "Between")
    slicer(p, 448, 280, col("Leads", "source"), "Source")
    slicer(p, 740, 280, col("Leads", "sales_rep"), "Sales rep")
    for i, (m, t) in enumerate([("Lead Count", "Leads"), ("SLA Met %", "Answered within SLA"), ("Waiting Past SLA", "Waiting past SLA"), ("Median Reply Minutes", "Median reply (minutes)"), ("Avg Potential Score", "Average potential score")]):
        kpi(p, i, mea("Leads", m), t)
    p.add("clusteredColumnChart", 16, 232, 760, 238, {"Category": [col("Calendar", "week_start")], "Y": [mea("Leads", "Lead Count")]}, title="Leads per week")
    p.add("donutChart", 788, 232, 476, 238, {"Category": [col("Leads", "sla_status")], "Y": [mea("Leads", "Lead Count")]}, title="SLA outcome")
    p.add("stackedBarChart", 16, 482, 620, 226, {"Category": [col("Leads", "sales_rep")], "Y": [mea("Leads", "Lead Count")], "Series": [col("Leads", "sla_status")]}, title="Leads per rep by SLA outcome")
    p.add("clusteredBarChart", 648, 482, 616, 226, {"Category": [col("Leads", "source")], "Y": [mea("Leads", "SLA Met %")]}, title="SLA met by source", sort=(mea("Leads", "SLA Met %"), "Ascending"))
    d = Page("leads_detail", "Waiting and speed")
    d.heading("Leads: who needs a reply now", "Leads still waiting past their SLA, and how fast the rest were answered.")
    slicer(d, 16, 420, col("Calendar", "date"), "Date range", "Between")
    slicer(d, 448, 280, col("Leads", "sales_rep"), "Sales rep")
    slicer(d, 740, 280, col("Leads", "source"), "Source")
    # Rows only show while [Waiting Past SLA] is not blank, so this table lists exactly the leads to chase.
    d.add("tableEx", 16, 138, 760, 570, {"Values": [col("Leads", "lead_id"), col("Leads", "created_date"), col("Leads", "source"), col("Leads", "company"), col("Leads", "sales_rep"),
                                                    col("Leads", "potential_score"), col("Leads", "sla_due_at"), mea("Leads", "Waiting Past SLA")]},
          title="Leads waiting past their SLA", sort=(col("Leads", "sla_due_at"), "Ascending"))
    d.add("clusteredColumnChart", 788, 138, 476, 280, {"Category": [col("Leads", "reply_bucket")], "Y": [mea("Leads", "Lead Count")]}, title="Time to first reply")
    d.add("pivotTable", 788, 430, 476, 278, {"Rows": [col("Leads", "sales_rep")], "Columns": [col("Leads", "sla_status")], "Values": [mea("Leads", "Lead Count")]}, title="Rep by SLA outcome")
    return [p, d]


def channels() -> list[Page]:
    p = Page("channels_overview", "Overview")
    p.heading("Channels", "Marketing performance per channel. Money is shown for one currency at a time: pick a currency to see spend and revenue.")
    slicer(p, 16, 420, col("Calendar", "date"), "Date range", "Between")
    slicer(p, 448, 220, col("Channels", "channel"), "Channel")
    slicer(p, 680, 160, col("Channels", "currency"), "Currency")
    slicer(p, 852, 200, col("Channels", "paid_or_organic"), "Paid or organic")
    for i, (m, t) in enumerate([("Sessions", "Sessions"), ("Clicks", "Clicks"), ("Conversions", "Conversions"), ("Conversion Rate", "Conversion rate"), ("Spend", "Spend (one currency)")]):
        kpi(p, i, mea("Channels", m), t)
    p.add("lineChart", 16, 232, 760, 238, {"Category": [col("Calendar", "date")], "Y": [mea("Channels", "Sessions")], "Series": [col("Channels", "channel")]}, title="Sessions per day")
    p.add("clusteredBarChart", 788, 232, 476, 238, {"Category": [col("Channels", "channel")], "Y": [mea("Channels", "Conversions")]}, title="Conversions by channel", sort=(mea("Channels", "Conversions"), "Descending"))
    p.add("tableEx", 16, 482, 1248, 226, {"Values": [col("Channels", "channel"), mea("Channels", "Sessions"), mea("Channels", "Clicks"), mea("Channels", "CTR"), mea("Channels", "Conversions"),
                                                   mea("Channels", "Conversion Rate"), mea("Channels", "Spend"), mea("Channels", "Revenue"), mea("Channels", "Cost per Conversion")]}, title="Per channel")
    d = Page("channels_detail", "Trend")
    d.heading("Channels: trend", "Month by month, and how paid compares with organic.")
    slicer(d, 16, 420, col("Calendar", "date"), "Date range", "Between")
    slicer(d, 448, 220, col("Channels", "channel"), "Channel")
    slicer(d, 680, 160, col("Channels", "currency"), "Currency")
    d.add("pivotTable", 16, 138, 760, 570, {"Rows": [col("Channels", "channel")], "Columns": [col("Calendar", "year_month")], "Values": [mea("Channels", "Conversions")]}, title="Conversions by channel and month")
    d.add("clusteredColumnChart", 788, 138, 476, 280, {"Category": [col("Channels", "paid_or_organic")], "Y": [mea("Channels", "Sessions")], "Series": [col("Channels", "medium")]}, title="Sessions: paid or organic")
    d.add("clusteredColumnChart", 788, 430, 476, 278, {"Category": [col("Calendar", "year_month")], "Y": [mea("Channels", "Sessions vs Previous Month")]}, title="Sessions vs previous month")
    return [p, d]


REPORTS = {"Social": social, "Leads": leads, "Channels": channels}


# ============================================================================================ check
def check(pages_by_report: dict[str, list[Page]]) -> list[str]:
    """Every Entity/Property a visual uses must exist in MODEL, as a column when asked as one and as a measure when asked as one."""
    cols = {t: {c[0] for c in s["columns"]} for t, s in MODEL.items()}
    meas = {t: {m[0] for m in s["measures"]} for t, s in MODEL.items()}
    bad = []
    for rep, pages in pages_by_report.items():
        for p in pages:
            for v in p.visuals:
                q = v["visual"].get("query")
                if not q:
                    continue
                for role, body in q["queryState"].items():
                    for pr in body["projections"]:
                        kind = "Column" if "Column" in pr["field"] else "Measure"
                        t, prop = pr["field"][kind]["Expression"]["SourceRef"]["Entity"], pr["field"][kind]["Property"]
                        ok = prop in (cols if kind == "Column" else meas).get(t, set())
                        if not ok:
                            bad.append(f"{rep}/{p.name}/{v['name']}: {kind} {t}[{prop}] is not in the model")
    for t, s in MODEL.items():
        names = [c[0] for c in s["columns"]] + [m[0] for m in s["measures"]]
        if len(names) != len(set(names)):
            bad.append(f"{t}: duplicate column/measure name")
        for _n, dax, _f in s["measures"]:
            for ref in __import__("re").findall(r"\[([^\]]+)\]", dax):
                if ref not in meas.get(t, set()) and ref not in {n for x in MODEL.values() for n in [c[0] for c in x["columns"]] + [m[0] for m in x["measures"]]}:
                    bad.append(f"{t}: DAX refers to unknown [{ref}]")
    return bad


def check_schema_urls(root: Path) -> list[str]:
    """Power BI Desktop rejects a report file whose $schema is not under .../fabric/item/report/<kind>/<n>.x.x/schema.json."""
    import re
    pat = re.compile(r"^https://developer\.microsoft\.com/json-schemas/fabric/item/report/[A-Za-z/]+/\d+\.\d+\.\d+/schema\.json$")
    bad = []
    for f in root.glob("*.Report/**/*.json"):
        if "StaticResources" in f.parts:
            continue
        sch = json.loads(f.read_text(encoding="utf-8")).get("$schema")
        if not sch or not pat.match(sch):
            bad.append(f"{f.relative_to(root)}: $schema {sch!r} does not match the report schema pattern")
    for f in root.glob("*.Report/definition.pbir"):
        if "/definitionProperties/" not in json.loads(f.read_text(encoding="utf-8")).get("$schema", ""):
            bad.append(f"{f.name}: wrong $schema")
    return bad


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    built = {name: fn() for name, fn in REPORTS.items()}
    problems = check(built)
    if problems:
        print("\n".join(problems))
        return 1
    model = ROOT / "ERPDesk.SemanticModel"
    if model.exists():
        shutil.rmtree(model)
    write_model(model)
    th = theme()
    (ROOT / "ERPDesk.theme.json").write_text(json.dumps(th, indent=2) + "\n", encoding="utf-8")
    for name, pages in built.items():
        write_report(ROOT / f"{name}.Report", pages, th)
        (ROOT / f"{name}.pbip").write_text(json.dumps({"version": "1.0", "artifacts": [{"report": {"path": f"{name}.Report"}}], "settings": {"enableAutoRecovery": True}}, indent=2) + "\n", encoding="utf-8")
    bad = check_schema_urls(ROOT)
    if bad:
        print("\n".join(bad))
        return 1
    n = sum(len(p.visuals) for ps in built.values() for p in ps)
    print(f"wrote model ({len(MODEL)} tables, {sum(len(s['measures']) for s in MODEL.values())} measures), {len(built)} reports, {sum(len(ps) for ps in built.values())} pages, {n} visuals")
    return 0


if __name__ == "__main__":
    sys.exit(main())
