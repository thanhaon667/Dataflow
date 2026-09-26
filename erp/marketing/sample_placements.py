"""Write a SYNTHETIC display-network placement CSV (the placement_performance export shape) so the Placements feature can be tried.

Everything in the file is invented: sites live under the reserved `.example` domain, apps are `app:com.example.*`, campaigns are called
"Synthetic ...". It is skewed like a real report (a few placements take most of the spend), it plants a handful of WASTEFUL placements (real spend, no
conversions), a few EFFICIENT ones (cheap conversions) and a few placements whose ads are mostly not seen (poor viewability), so most Insights rules have something to find.
Nothing is loaded: the file is written to data_inbox/ (git-ignored, and NOT the autorun inbox data_inbox/incoming), and you decide what to do with it:

    venv\\Scripts\\python.exe -m erp.marketing.sample_placements --rows 5000
    venv\\Scripts\\python.exe -m erp.marketing.ingest --connector placement_performance --csv data_inbox\\synthetic_placements_5000.csv --dry-run
    venv\\Scripts\\python.exe -m erp.marketing.sample_placements --rows 20000 --days 45 --currency EUR --seed 7 --out C:\\temp\\demo.csv

Deterministic: the same --rows, --days, --seed and --end-date give the same file, byte for byte. Standard library only.
"""
from __future__ import annotations

import argparse
import csv
import logging
import math
import random
import sys
from datetime import date, timedelta
from pathlib import Path

logger = logging.getLogger("erp.marketing.sample_placements")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DIR = PROJECT_ROOT / "data_inbox"
HEADER = ["Date", "Campaign", "Ad group", "Placement", "Placement type", "Ad size", "Position", "Device", "Impressions", "Clicks", "Cost",
          "Conversions", "Conversion value", "Viewable impressions"]
CAMPAIGNS = [("Synthetic Spring Sale", ("Shoppers", "Lookalike")), ("Synthetic Brand Awareness", ("Broad reach", "Interest")),
             ("Synthetic Retargeting", ("Cart visitors", "Past buyers"))]
SIZES = [("300x250", 3.0, 0.9), ("728x90", 2.0, 0.6), ("320x50", 2.5, 0.5), ("160x600", 1.0, 0.6), ("300x600", 1.0, 0.8), ("970x250", 0.8, 1.0)]  # size, weight, cpm factor
DEVICES = [("mobile", 0.6), ("desktop", 0.3), ("tablet", 0.1)]
MAX_ROWS = 2_000_000


def _weighted(rng: random.Random, items):
    total = sum(w for _v, w in items)
    x = rng.random() * total
    for v, w in items:
        x -= w
        if x <= 0:
            return v
    return items[-1][0]


def build_placements(rng: random.Random, n: int) -> list[dict]:
    """A pool of placements with a power-law weight and a hidden quality: 'wasteful' (never converts), 'efficient' (cheap conversions), 'normal'."""
    pool = []
    for i in range(n):
        kind = _weighted(rng, (("website", 0.55), ("app", 0.35), ("video", 0.10)))
        name = {"website": f"news-site-{i:03d}.example", "app": f"app:com.example.game{i:03d}", "video": f"video-hub-{i:03d}.example/embed"}[kind]
        pool.append({"name": name, "type": kind, "weight": 1.0 / math.pow(i + 1, 0.9) * rng.uniform(0.6, 1.4), "quality": "normal",
                     "measured": rng.random() > 0.08})
    heavy = sorted(range(n), key=lambda k: -pool[k]["weight"])
    for k in heavy[1:4]:                                   # big spenders that never convert
        pool[k]["quality"] = "wasteful"
    for k in heavy[6:9]:                                   # mid-sized and cheap per conversion
        pool[k]["quality"] = "efficient"
    for k in heavy[9:12]:                                  # big, measured, and mostly not seen
        pool[k]["quality"], pool[k]["measured"] = "unseen", True
    return pool


def make_rows(rows: int, days: int, seed: int, currency: str, end: date) -> list[list]:
    rng = random.Random(seed)
    pool = build_placements(rng, max(12, min(400, rows // 12)))
    weights = [(p, p["weight"]) for p in pool]
    used: set = set()
    out: list[list] = []
    tries = 0
    while len(out) < rows and tries < rows * 20:
        tries += 1
        p = _weighted(rng, weights)
        campaign, groups = CAMPAIGNS[rng.randrange(len(CAMPAIGNS))]
        group = groups[rng.randrange(len(groups))]
        size, _w, cpm_factor = _weighted(rng, [((s, w, c), w) for s, w, c in SIZES])
        if p["type"] == "app" and size in ("728x90", "160x600", "970x250"):
            size = "320x50"                                # apps do not run desktop banners
        position = "unknown" if not p["measured"] and rng.random() < 0.5 else ("above_fold" if rng.random() < 0.55 else "below_fold")
        device = _weighted(rng, DEVICES)
        day = end - timedelta(days=rng.randrange(days))
        key = (day, campaign, group, p["name"], size, position, device)
        if key in used:
            continue
        used.add(key)
        base = max(50.0, rng.lognormvariate(6.2, 0.9) * (0.5 + 3 * p["weight"]))
        impressions = int(base * (1.6 if p["quality"] == "wasteful" else 1.0))
        ctr = rng.uniform(0.002, 0.012) * (0.4 if position == "below_fold" else 1.0) * (0.25 if p["quality"] == "wasteful" and rng.random() < 0.5 else 1.0)
        clicks = min(impressions, int(round(impressions * ctr)))
        cpm = rng.uniform(1.5, 4.5) * cpm_factor * (1.0 if p["quality"] != "efficient" else 0.6)
        cost = impressions / 1000 * cpm
        cvr = {"wasteful": 0.0, "efficient": rng.uniform(0.05, 0.09), "normal": rng.uniform(0.008, 0.03)}.get(p["quality"], 0.02)
        conversions = sum(1 for _ in range(min(clicks, 400)) if rng.random() < cvr) if clicks else 0
        value = conversions * rng.uniform(25, 60)
        viewable = ""
        if p["measured"] and (position != "unknown" or p["quality"] == "unseen"):
            rate = (0.72 if position == "above_fold" else 0.30) * rng.uniform(0.85, 1.1)
            if p["quality"] == "unseen":
                rate = rng.uniform(0.12, 0.30)
            viewable = str(min(impressions, int(impressions * rate)))
        out.append([day.isoformat(), campaign, group, p["name"], p["type"], size, position, device, impressions, clicks,
                    f"{cost:.2f} {currency}", conversions, f"{value:.2f} {currency}", viewable])
    out.sort(key=lambda r: (r[0], r[1], r[2], r[3], r[5], r[6], r[7]))
    return out


def write_csv(path: Path, rows: list[list]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(HEADER)
        w.writerows(rows)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m erp.marketing.sample_placements",
                                description="Write a SYNTHETIC placement_performance CSV (invented sites, apps and numbers) to try the Placements feature. Loads nothing.")
    p.add_argument("--rows", type=int, default=5000, help="number of rows (default 5000)")
    p.add_argument("--days", type=int, default=30, help="the rows are spread over this many days ending at --end-date (default 30)")
    p.add_argument("--end-date", default=None, help="last day, YYYY-MM-DD (default: yesterday)")
    p.add_argument("--currency", default="EUR", help="ISO code written on every Cost cell (default EUR)")
    p.add_argument("--seed", type=int, default=1, help="random seed: the same arguments always write the same file (default 1)")
    p.add_argument("--out", default=None, help="output file (default data_inbox/synthetic_placements_<rows>.csv)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 1 <= args.rows <= MAX_ROWS:
        print(f"error: --rows must be between 1 and {MAX_ROWS:,}", file=sys.stderr)
        return 2
    if not 1 <= args.days <= 3660:
        print("error: --days must be between 1 and 3660", file=sys.stderr)
        return 2
    cur = (args.currency or "").strip().upper()
    if len(cur) != 3 or not cur.isalpha():
        print("error: --currency must be a three-letter ISO 4217 code", file=sys.stderr)
        return 2
    try:
        end = date.fromisoformat(args.end_date) if args.end_date else date.today() - timedelta(days=1)
    except ValueError:
        print("error: --end-date must be YYYY-MM-DD", file=sys.stderr)
        return 2
    out = Path(args.out) if args.out else DEFAULT_DIR / f"synthetic_placements_{args.rows}.csv"
    rows = make_rows(args.rows, args.days, args.seed, cur, end)
    write_csv(out, rows)
    print(f"Wrote {len(rows):,} SYNTHETIC placement rows ({rows[0][0]} to {rows[-1][0]}, currency {cur}) to {out}")
    print("Every site, app, campaign and number in it is invented. Nothing was loaded. To look at it without loading:")
    print(f"  venv\\Scripts\\python.exe -m erp.marketing.ingest --connector placement_performance --csv \"{out}\" --dry-run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
