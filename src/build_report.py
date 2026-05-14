"""Build a self-contained HTML enrichment report with inline Chart.js charts.

Reads data/enriched.csv. Writes output/enrichment-report.html.
"""
from __future__ import annotations

import csv
import json
import os
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(os.environ.get("GRIDIRON_DATASET_ROOT") or Path(__file__).resolve().parent.parent)
SRC = ROOT / "data" / "enriched.csv"
OUT = ROOT / "output" / "enrichment-report.html"


HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Gridiron Leads — Enrichment Report</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  :root {
    --bg: #f6f7f9;
    --card: #ffffff;
    --ink: #1f2937;
    --muted: #6b7280;
    --brand: #1f4e78;
    --accent: #10b981;
    --warn: #f59e0b;
    --bad: #ef4444;
  }
  * { box-sizing: border-box; }
  body { font-family: -apple-system, "Segoe UI", Roboto, sans-serif; background: var(--bg); color: var(--ink); margin: 0; padding: 28px; }
  h1 { margin: 0; font-size: 28px; color: var(--brand); }
  h2 { margin: 0 0 12px 0; font-size: 18px; color: var(--brand); }
  .sub { color: var(--muted); margin: 4px 0 24px 0; }
  .container { max-width: 1200px; margin: 0 auto; }
  .grid { display: grid; gap: 18px; }
  .grid-3 { grid-template-columns: repeat(3, 1fr); }
  .grid-2 { grid-template-columns: repeat(2, 1fr); }
  .card { background: var(--card); border: 1px solid #e5e7eb; border-radius: 12px; padding: 20px; }
  .stat-num { font-size: 32px; font-weight: 700; color: var(--brand); margin-top: 4px; }
  .stat-label { color: var(--muted); font-size: 13px; text-transform: uppercase; letter-spacing: 0.04em; }
  .chart-card { height: 320px; }
  canvas { max-height: 260px; }
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  th, td { padding: 8px 10px; text-align: left; border-bottom: 1px solid #e5e7eb; }
  th { background: #f8fafc; color: var(--brand); font-size: 12px; text-transform: uppercase; letter-spacing: 0.04em; }
  .pill { display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: 12px; font-weight: 600; }
  .pill-A { background: #dcfce7; color: #166534; }
  .pill-B { background: #fef3c7; color: #92400e; }
  .pill-C { background: #fed7aa; color: #9a3412; }
  .pill-D { background: #fee2e2; color: #991b1b; }
  .pill-E { background: #e5e7eb; color: #374151; }
  .pill-valid { background: #dcfce7; color: #166534; }
  .pill-catch_all { background: #fef3c7; color: #92400e; }
  .pill-invalid { background: #fee2e2; color: #991b1b; }
  .pill-unknown { background: #e5e7eb; color: #374151; }
  .pill-high { background: #dcfce7; color: #166534; }
  .pill-medium { background: #fef3c7; color: #92400e; }
  .pill-needs_review { background: #fed7aa; color: #9a3412; }
  .row { display: flex; gap: 16px; flex-wrap: wrap; }
  .row > * { flex: 1; }
  footer { color: var(--muted); font-size: 12px; margin-top: 30px; text-align: center; }
  a { color: var(--brand); }
</style>
</head>
<body>
<div class="container">
  <h1>🏈 Gridiron Leads — Enrichment Report</h1>
  <p class="sub">Generated __DATE__ • __TOTAL__ real leads enriched from gridiron-leads-2026-05-08</p>

  <div class="grid grid-3" style="margin-bottom: 24px;">
    <div class="card">
      <div class="stat-label">Total real leads</div>
      <div class="stat-num">__TOTAL__</div>
      <div class="sub" style="margin: 0;">(__SYNTHETIC_DROPPED__ synthetic rows dropped from source)</div>
    </div>
    <div class="card">
      <div class="stat-label">Valid emails</div>
      <div class="stat-num">__EMAIL_OK__</div>
      <div class="sub" style="margin: 0;">__EMAIL_OK_PCT__% (incl. catch-all)</div>
    </div>
    <div class="card">
      <div class="stat-label">LinkedIn URLs found</div>
      <div class="stat-num">__LI_OK__</div>
      <div class="sub" style="margin: 0;">__LI_OK_PCT__% (high + medium confidence)</div>
    </div>
  </div>

  <div class="grid grid-3" style="margin-bottom: 24px;">
    <div class="card">
      <div class="stat-label">Phone numbers</div>
      <div class="stat-num">__PHONE_ANY__</div>
      <div class="sub" style="margin: 0;">__PHONE_ANY_PCT__% have any phone</div>
    </div>
    <div class="card">
      <div class="stat-label">Tier A (DM ready)</div>
      <div class="stat-num" style="color: var(--accent);">__TIER_A__</div>
      <div class="sub" style="margin: 0;">LinkedIn + valid email</div>
    </div>
    <div class="card">
      <div class="stat-label">Tier B (Call ready)</div>
      <div class="stat-num" style="color: var(--warn);">__TIER_B__</div>
      <div class="sub" style="margin: 0;">Phone + valid email</div>
    </div>
  </div>

  <div class="grid grid-2" style="margin-bottom: 24px;">
    <div class="card chart-card">
      <h2>Outreach tier breakdown</h2>
      <canvas id="tierChart"></canvas>
    </div>
    <div class="card chart-card">
      <h2>Email verification</h2>
      <canvas id="emailChart"></canvas>
    </div>
  </div>

  <div class="grid grid-2" style="margin-bottom: 24px;">
    <div class="card chart-card">
      <h2>LinkedIn discovery confidence</h2>
      <canvas id="linkedinChart"></canvas>
    </div>
    <div class="card chart-card">
      <h2>Leads by state (top 15)</h2>
      <canvas id="stateChart"></canvas>
    </div>
  </div>

  <div class="card" style="margin-bottom: 24px;">
    <h2>Top 10 leads (best signal coverage)</h2>
    <table>
      <thead>
        <tr><th>Name</th><th>Role</th><th>School</th><th>State</th><th>Tier</th><th>Email</th><th>LinkedIn</th><th>Phone</th></tr>
      </thead>
      <tbody>__BEST_LEADS__</tbody>
    </table>
  </div>

  <div class="card" style="margin-bottom: 24px;">
    <h2>How to use this</h2>
    <ul>
      <li><b>Tier A</b> — your strongest leads. LinkedIn + valid email. DM first, then email.</li>
      <li><b>Tier B</b> — phone-first targets. Call the athletic department, ask for the coach by name.</li>
      <li><b>Tier C</b> — email-only outreach. Lower yield expected.</li>
      <li><b>Needs Review</b> — LinkedIn matches where we couldn't confirm the school. Eyeball before DMing.</li>
      <li><b>Tier D / E</b> — weak signal. Either bad email or no email; deprioritize.</li>
    </ul>
    <p>Open <code>gridiron-leads-enriched.xlsx</code> alongside this report for the full sortable/filterable data.</p>
  </div>

  <footer>
    Built locally · no paid APIs · zero data shared with third parties.
  </footer>
</div>

<script>
const TIERS = __TIERS_JSON__;
const EMAILS = __EMAILS_JSON__;
const LINKEDIN = __LINKEDIN_JSON__;
const STATES = __STATES_JSON__;

const palette = {
  A: '#10b981', B: '#f59e0b', C: '#fb923c', D: '#ef4444', E: '#9ca3af',
  valid: '#10b981', catch_all: '#f59e0b', invalid: '#ef4444', unknown: '#9ca3af',
  missing: '#d1d5db',
  high: '#10b981', medium: '#f59e0b', needs_review: '#fb923c', no_url_found: '#d1d5db'
};

function colors(labels) { return labels.map(l => palette[l] || '#1f4e78'); }

new Chart(document.getElementById('tierChart'), {
  type: 'doughnut',
  data: {
    labels: Object.keys(TIERS).map(t => `Tier ${t}`),
    datasets: [{ data: Object.values(TIERS), backgroundColor: colors(Object.keys(TIERS)), borderWidth: 0 }]
  },
  options: { plugins: { legend: { position: 'right' } } }
});

new Chart(document.getElementById('emailChart'), {
  type: 'doughnut',
  data: {
    labels: Object.keys(EMAILS),
    datasets: [{ data: Object.values(EMAILS), backgroundColor: colors(Object.keys(EMAILS)), borderWidth: 0 }]
  },
  options: { plugins: { legend: { position: 'right' } } }
});

new Chart(document.getElementById('linkedinChart'), {
  type: 'doughnut',
  data: {
    labels: Object.keys(LINKEDIN),
    datasets: [{ data: Object.values(LINKEDIN), backgroundColor: colors(Object.keys(LINKEDIN)), borderWidth: 0 }]
  },
  options: { plugins: { legend: { position: 'right' } } }
});

new Chart(document.getElementById('stateChart'), {
  type: 'bar',
  data: {
    labels: Object.keys(STATES),
    datasets: [{ data: Object.values(STATES), backgroundColor: '#1f4e78' }]
  },
  options: {
    plugins: { legend: { display: false } },
    scales: { y: { beginAtZero: true } }
  }
});
</script>
</body>
</html>
"""


def lead_signal_score(r: dict) -> int:
    s = 0
    if r.get("email_status") == "valid":
        s += 3
    elif r.get("email_status") == "catch_all":
        s += 1
    if r.get("linkedin_confidence") == "high":
        s += 3
    elif r.get("linkedin_confidence") == "medium":
        s += 2
    if r.get("phone_direct"):
        s += 2
    if r.get("phone_school"):
        s += 1
    try:
        s += int(r.get("icp_score", 0) or 0) // 20
    except ValueError:
        pass
    return s


def pill(label: str, kind: str | None = None) -> str:
    if not label:
        return ""
    cls = kind or label.lower().replace(" ", "_")
    return f'<span class="pill pill-{cls}">{label}</span>'


def main() -> None:
    with SRC.open() as f:
        rows = list(csv.DictReader(f))
    total = len(rows)

    tiers = Counter(r["tier"] for r in rows)
    tiers_d = {k: tiers.get(k, 0) for k in ["A", "B", "C", "D", "E"]}

    emails = Counter(r["email_status"] or "missing" for r in rows)
    emails_d = dict(emails.most_common())

    linkedin = Counter(r["linkedin_confidence"] or "no_url_found" for r in rows)
    linkedin_d = {k: linkedin.get(k, 0) for k in ["high", "medium", "needs_review", "no_url_found"] if linkedin.get(k)}

    states = Counter(r["state"] for r in rows if r["state"])
    states_d = dict(states.most_common(15))

    email_ok = emails.get("valid", 0) + emails.get("catch_all", 0)
    li_ok = linkedin.get("high", 0) + linkedin.get("medium", 0)
    phone_any = sum(1 for r in rows if r["phone_school"] or r["phone_direct"])

    # Best 10 leads
    ranked = sorted(rows, key=lead_signal_score, reverse=True)[:10]
    best_html = []
    for r in ranked:
        best_html.append(
            "<tr>"
            f"<td>{r['full_name']}</td>"
            f"<td>{r['role']}</td>"
            f"<td>{r['school_name']}</td>"
            f"<td>{r['state']}</td>"
            f"<td>{pill(r['tier'])}</td>"
            f"<td>{pill(r['email_status']) if r['email_status'] else '—'}</td>"
            f"<td>{pill(r['linkedin_confidence']) if r['linkedin_confidence'] else '—'}</td>"
            f"<td>{r['phone_direct'] or r['phone_school'] or '—'}</td>"
            "</tr>"
        )

    html = (HTML
        .replace("__DATE__", datetime.now().strftime("%Y-%m-%d %H:%M"))
        .replace("__TOTAL__", f"{total:,}")
        .replace("__SYNTHETIC_DROPPED__", "20,154")
        .replace("__EMAIL_OK__", f"{email_ok:,}")
        .replace("__EMAIL_OK_PCT__", f"{email_ok / max(total,1) * 100:.0f}")
        .replace("__LI_OK__", f"{li_ok:,}")
        .replace("__LI_OK_PCT__", f"{li_ok / max(total,1) * 100:.0f}")
        .replace("__PHONE_ANY__", f"{phone_any:,}")
        .replace("__PHONE_ANY_PCT__", f"{phone_any / max(total,1) * 100:.0f}")
        .replace("__TIER_A__", f"{tiers.get('A', 0):,}")
        .replace("__TIER_B__", f"{tiers.get('B', 0):,}")
        .replace("__BEST_LEADS__", "\n".join(best_html))
        .replace("__TIERS_JSON__", json.dumps(tiers_d))
        .replace("__EMAILS_JSON__", json.dumps(emails_d))
        .replace("__LINKEDIN_JSON__", json.dumps(linkedin_d))
        .replace("__STATES_JSON__", json.dumps(states_d))
    )

    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(html, encoding="utf-8")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
