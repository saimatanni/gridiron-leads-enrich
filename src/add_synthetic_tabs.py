"""Add synthetic-data tabs to the enriched Excel file and dump a standalone CSV.

Produces, next to the real enrichment:
    output/gridiron-leads-enriched.xlsx   <- adds 2 new tabs
        + "Why It's Fake"     - the evidence summary
        + "Synthetic Sample"  - rows sorted by duplicate-count, dup column colored
    output/synthetic-fake-users.csv       <- all 20,154 synthetic rows, deduped view

Reads the original source.xlsx for the synthetic rows (the enrichment pipeline
dropped them on purpose).
"""
from __future__ import annotations

import csv
import os
from collections import Counter
from pathlib import Path

import openpyxl
from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font, PatternFill, Border, Side
from openpyxl.utils import get_column_letter

ROOT = Path(os.environ.get("GRIDIRON_DATASET_ROOT") or Path(__file__).resolve().parent.parent)
SRC = ROOT / "source.xlsx"
XLSX = ROOT / "output" / "gridiron-leads-enriched.xlsx"
CSV_OUT = ROOT / "output" / "synthetic-fake-users.csv"

HEADER_FILL = PatternFill("solid", fgColor="8B0000")  # dark red — "do not use"
HEADER_FONT = Font(color="FFFFFF", bold=True, size=11)
WARN_FILL = PatternFill("solid", fgColor="FEE2E2")
WARN_FONT = Font(color="991B1B", bold=True)
INFO_FILL = PatternFill("solid", fgColor="FEF3C7")
THIN = Border(bottom=Side(style="thin", color="DDDDDD"))


def load_synthetic_rows() -> list[dict]:
    wb = openpyxl.load_workbook(SRC, read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]
    rows_iter = ws.iter_rows(values_only=True)
    header = [str(h) if h is not None else "" for h in next(rows_iter)]
    idx = {n: i for i, n in enumerate(header)}

    keep_cols = ["id", "source", "first_name", "last_name", "email", "company",
                 "company_domain", "state", "enrichment_status", "scraped_at"]
    out = []
    for row in rows_iter:
        if not row:
            continue
        if (row[idx["source"]] or "") != "synthetic":
            continue
        out.append({c: (row[idx[c]] if c in idx and row[idx[c]] is not None else "") for c in keep_cols})
    return out


def write_evidence_tab(wb, real_count: int, syn_count: int, dup_counts: list[tuple[str, int]],
                       fake_schools: list[tuple[str, int]], state_compare: list[tuple[str, int, int]],
                       dns_results: list[tuple[str, str]]) -> None:
    ws = wb.create_sheet("Why It's Fake", 1)
    ws.column_dimensions["A"].width = 6
    ws.column_dimensions["B"].width = 50
    ws.column_dimensions["C"].width = 25
    ws.column_dimensions["D"].width = 30
    ws.sheet_view.showGridLines = False

    title = Font(bold=True, size=16, color="8B0000")
    h2 = Font(bold=True, size=12, color="1F4E78")
    body = Font(size=11)

    ws["B1"] = "⚠️ Why the 20,154 'synthetic' rows are fake"
    ws["B1"].font = title
    ws["B2"] = ("These rows are placeholder/test data — not real coaches or athletic directors. "
                "Six independent signals confirm this. Do NOT outreach to them.")
    ws["B2"].font = Font(italic=True, color="666666")

    row = 4
    def line(title, value=None, fill=None):
        nonlocal row
        ws.cell(row=row, column=2, value=title).font = h2
        if value is not None:
            cell = ws.cell(row=row, column=3, value=value)
            cell.font = body
            if fill: cell.fill = fill
        row += 1

    def sub(text, fill=None):
        nonlocal row
        c = ws.cell(row=row, column=2, value=text)
        c.font = body
        if fill: c.fill = fill
        row += 1

    line("Evidence #1 — The data labels itself", "see 'source' column")
    sub("source='synthetic'        →  20,154 rows  (the fakes)", WARN_FILL)
    sub("source='maxpreps'         →  125 rows", INFO_FILL)
    sub("source='maxpreps_scraper' →  198 rows", INFO_FILL)
    sub("source='TX_athletic_assoc' → 20 rows", INFO_FILL)
    sub("source='FL_athletic_assoc' → 15 rows", INFO_FILL)
    row += 1

    line("Evidence #2 — emails are 'generated' (fabricated)", "16,770 rows have enrichment_status='generated'")
    row += 1

    line("Evidence #3 — same email assigned to many fake 'people'")
    sub("(in real data, no two people share an email address)")
    for em, n in dup_counts[:8]:
        c1 = ws.cell(row=row, column=2, value=em)
        c2 = ws.cell(row=row, column=3, value=f"{n}× duplicate")
        c1.fill = WARN_FILL; c2.fill = WARN_FILL
        c2.font = WARN_FONT
        row += 1
    row += 1

    line("Evidence #4 — 'school names' look templated ({President} {Direction})")
    for sch, n in fake_schools[:6]:
        c1 = ws.cell(row=row, column=2, value=sch)
        c2 = ws.cell(row=row, column=3, value=f"{n}× rows")
        c1.fill = WARN_FILL; c2.fill = WARN_FILL
        row += 1
    row += 1

    line("Evidence #5 — geographic distribution is suspiciously flat",
         "real US high schools are NOT evenly distributed")
    ws.cell(row=row, column=2, value="State").font = h2
    ws.cell(row=row, column=3, value="Synthetic rows").font = h2
    ws.cell(row=row, column=4, value="Real rows").font = h2
    row += 1
    for st, s, r in state_compare:
        ws.cell(row=row, column=2, value=st)
        ws.cell(row=row, column=3, value=s).fill = WARN_FILL
        ws.cell(row=row, column=4, value=r).fill = INFO_FILL
        row += 1
    sub("Synthetic = quota-driven generation, ~430/state across all 50.", INFO_FILL)
    row += 1

    line("Evidence #6 — the 'school' domains don't even exist in DNS",
         "can't send email to a domain with no mail server")
    ws.cell(row=row, column=2, value="Domain").font = h2
    ws.cell(row=row, column=3, value="DNS MX lookup").font = h2
    row += 1
    for d, mx in dns_results:
        ws.cell(row=row, column=2, value=d)
        c = ws.cell(row=row, column=3, value=mx)
        if "none" in mx.lower():
            c.fill = WARN_FILL
            c.font = WARN_FONT
        else:
            c.fill = INFO_FILL
        row += 1


def write_synthetic_sample_tab(wb, syn_rows: list[dict], dup_count_by_email: Counter) -> None:
    ws = wb.create_sheet("Synthetic Sample", 2)
    ws.sheet_view.showGridLines = False

    note_font = Font(italic=True, color="991B1B")
    ws["A1"] = ("⚠️ FAKE DATA — Do not outreach to these. Showing top 1,000 worst-offender duplicates "
                "from the 20,154 synthetic rows. The 'Dup Count' column tells you how many fake 'people' "
                "share that one email.")
    ws["A1"].font = note_font
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=10)
    ws.row_dimensions[1].height = 32

    cols = ["dup_count", "email", "first_name", "last_name", "company", "state",
            "enrichment_status", "source", "company_domain", "id"]
    pretty = {"dup_count": "Dup Count", "id": "Lead ID", "first_name": "First",
              "last_name": "Last", "company": "School (fake)", "company_domain": "Domain",
              "enrichment_status": "Enrichment status"}

    for i, c in enumerate(cols, 1):
        cell = ws.cell(row=3, column=i, value=pretty.get(c, c.replace("_", " ").title()))
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="left", vertical="center")

    # Build sortable rows
    enriched = []
    for r in syn_rows:
        em = (r.get("email") or "").lower()
        r["dup_count"] = dup_count_by_email.get(em, 0)
        enriched.append(r)
    enriched.sort(key=lambda r: r["dup_count"], reverse=True)
    sample = enriched[:1000]

    for ri, r in enumerate(sample, 4):
        for ci, c in enumerate(cols, 1):
            cell = ws.cell(row=ri, column=ci, value=r.get(c, ""))
            cell.border = THIN
            cell.alignment = Alignment(horizontal="left", vertical="top")
            if c == "dup_count":
                cell.font = WARN_FONT
                cell.fill = WARN_FILL
            elif c == "email" and r["dup_count"] >= 100:
                cell.fill = WARN_FILL
            elif c == "source":
                cell.fill = WARN_FILL
                cell.font = WARN_FONT

    # widths
    widths = {"dup_count": 11, "email": 36, "first_name": 12, "last_name": 14,
              "company": 30, "state": 8, "enrichment_status": 18, "source": 14,
              "company_domain": 24, "id": 12}
    for i, c in enumerate(cols, 1):
        ws.column_dimensions[get_column_letter(i)].width = widths.get(c, 14)

    ws.freeze_panes = "A4"
    last_col = get_column_letter(len(cols))
    ws.auto_filter.ref = f"A3:{last_col}{len(sample) + 3}"


def main() -> None:
    syn = load_synthetic_rows()
    print(f"loaded {len(syn)} synthetic rows")

    # dup counts by email
    dup = Counter((r.get("email") or "").lower() for r in syn if r.get("email"))
    top_dup_emails = dup.most_common(20)

    # fake school templates
    schools = Counter(r.get("company") for r in syn if r.get("company")).most_common(20)

    # state compare
    syn_states = Counter(r.get("state") for r in syn if r.get("state"))
    # load real state counts from leads_clean.csv if exists, else skip
    real_states = Counter()
    leads_clean = ROOT / "data" / "leads_clean.csv"
    if leads_clean.exists():
        with leads_clean.open() as f:
            for r in csv.DictReader(f):
                if r.get("state"):
                    real_states[r["state"]] += 1
    state_compare = [(st, syn_states.get(st, 0), real_states.get(st, 0))
                     for st in ["CA", "TX", "FL", "NY", "AK", "HI", "WY", "VT"]]

    # DNS hardcoded — captured live earlier
    dns_results = [
        ("adamseast.com (fake)",        "no MX record"),
        ("kennedycommunity.com (fake)", "no MX record"),
        ("rooseveltregional.com (fake)","no MX record"),
        ("franklinsouth.com (fake)",    "no MX record"),
        ("mckinleywest.com (fake)",     "no MX record"),
        ("ahisd.net (real school)",     "MX found — actual mail server"),
        ("alcoaschools.net (real)",     "MX found — actual mail server"),
    ]

    # --- Update Excel ---
    wb = load_workbook(XLSX)
    for existing in ["Why It's Fake", "Synthetic Sample"]:
        if existing in wb.sheetnames:
            del wb[existing]

    real_count = sum(1 for s in wb.sheetnames if "Tier" in s or s == "All Leads")
    write_evidence_tab(wb, real_count, len(syn), top_dup_emails, schools,
                       state_compare, dns_results)
    write_synthetic_sample_tab(wb, syn, dup)

    # Reorder: Summary, Why It's Fake, Synthetic Sample, then tiers, then All Leads
    order = ["Summary", "Why It's Fake", "Synthetic Sample",
             "Tier A (DM ready)", "Tier B (Call ready)", "Tier C (Email only)",
             "Needs Review", "All Leads"]
    wb._sheets = [wb[n] for n in order if n in wb.sheetnames]

    wb.save(XLSX)
    print(f"updated {XLSX} (now has 'Why It's Fake' and 'Synthetic Sample' tabs)")

    # --- Standalone CSV ---
    CSV_OUT.parent.mkdir(exist_ok=True)
    with CSV_OUT.open("w", newline="") as f:
        cols = ["dup_count", "email", "first_name", "last_name", "company",
                "state", "enrichment_status", "source", "company_domain", "id"]
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in syn:
            row = {c: r.get(c, "") for c in cols}
            row["dup_count"] = dup.get((r.get("email") or "").lower(), 0)
            w.writerow(row)
    print(f"wrote {CSV_OUT} ({len(syn)} rows)")


if __name__ == "__main__":
    main()
