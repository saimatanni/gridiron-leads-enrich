"""Import phone-only leads from source CSV into an existing dataset.

Use when source has rows that have NO email but DO have first+last+phone+school.
These leads need no email_verify and no phone enrichment — they're already
complete on the cold-call side. We optionally run linkedin_find later to
discover their LinkedIn URL.

Reads:  $DATASET/source.xlsx
Appends to:
   $DATASET/data/leads_clean.csv
   $DATASET/data/phone_results.csv
   $DATASET/data/email_verify_results.csv   (empty status = skipped)
   $DATASET/data/linkedin_results.csv       (empty url = to be enriched)
"""
from __future__ import annotations

import csv
import hashlib
import os
import re
import sys
from pathlib import Path

import openpyxl

ROOT = Path(os.environ.get("GRIDIRON_DATASET_ROOT") or Path(__file__).resolve().parent.parent)
SRC = ROOT / "source.xlsx"
LEADS = ROOT / "data" / "leads_clean.csv"
PHONES = ROOT / "data" / "phone_results.csv"
EMAILS = ROOT / "data" / "email_verify_results.csv"
LI = ROOT / "data" / "linkedin_results.csv"

# Same canonical column set as extract.py
LEAD_COLS = [
    "lead_id", "tier", "first_name", "last_name", "full_name",
    "role", "role_category", "school_name", "school_domain",
    "city", "state", "email", "email_status",
    "linkedin_url", "linkedin_confidence",
    "phone_school", "phone_direct", "best_channel",
    "icp_tier", "icp_score", "enriched_at", "source_page", "notes",
]

PHONE_RE = re.compile(r"(?:\+?1[\s.-]?)?\(?(\d{3})\)?[\s.-]?(\d{3})[\s.-]?(\d{4})")


def normalize_phone(p: str) -> str:
    """Return (XXX) XXX-XXXX or '' if can't parse."""
    if not p:
        return ""
    m = PHONE_RE.search(p)
    if not m:
        return ""
    return f"({m.group(1)}) {m.group(2)}-{m.group(3)}"


def lead_id_for(first: str, last: str, school: str) -> str:
    """Deterministic lead_id for emailless leads — based on name+school."""
    key = f"{first.lower().strip()}|{last.lower().strip()}|{school.lower().strip()}"
    h = hashlib.md5(key.encode()).hexdigest()
    return f"lead_{h[:24]}"


def load_existing_lead_ids() -> set[str]:
    if not LEADS.exists():
        return set()
    with LEADS.open() as f:
        return {r["lead_id"] for r in csv.DictReader(f)}


def main() -> None:
    if not SRC.exists():
        print(f"ERROR: {SRC} not found", file=sys.stderr)
        sys.exit(1)

    wb = openpyxl.load_workbook(SRC, read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]
    rows = ws.iter_rows(values_only=True)
    header = [str(h or "").strip().lower() for h in next(rows)]

    def idx(*names):
        for n in names:
            if n in header:
                return header.index(n)
        return None

    i_first = idx("first_name", "firstname", "first name", "first")
    i_last  = idx("last_name", "lastname", "last name", "last")
    i_full  = idx("full_name", "fullname", "name")
    i_email = idx("email")
    i_phone = idx("phone", "_phone", "phone_number", "telephone")
    i_company = idx("company", "school", "school_name", "institute")
    i_title = idx("title", "role", "position", "designation")
    i_city  = idx("city")
    i_state = idx("state")

    if i_first is None or i_last is None or i_phone is None or i_company is None:
        print(f"ERROR: source missing required columns. Need first_name, last_name, phone, company. Have: {header}", file=sys.stderr)
        sys.exit(1)

    existing = load_existing_lead_ids()
    print(f"already in leads_clean.csv: {len(existing)} leads")

    seen_in_pass = set()
    new_leads = []
    new_phones = []
    new_emails = []
    new_lis = []

    dropped_has_email = 0
    dropped_no_phone = 0
    dropped_dup_in_pass = 0
    dropped_dup_with_existing = 0

    for row in rows:
        if not row:
            continue
        def g(i):
            return str(row[i] or "").strip() if i is not None and i < len(row) else ""

        email = g(i_email).lower()
        phone_raw = g(i_phone)
        if email:
            dropped_has_email += 1
            continue
        if not phone_raw:
            dropped_no_phone += 1
            continue

        first = g(i_first)
        last  = g(i_last)
        if not first or not last:
            full = g(i_full)
            if full:
                parts = full.split(maxsplit=1)
                first, last = parts[0], (parts[1] if len(parts) > 1 else "")
        if not first or not last:
            continue

        company = g(i_company)
        if not company:
            continue

        phone_n = normalize_phone(phone_raw)
        if not phone_n:
            continue

        lid = lead_id_for(first, last, company)
        if lid in seen_in_pass:
            dropped_dup_in_pass += 1
            continue
        if lid in existing:
            dropped_dup_with_existing += 1
            continue
        seen_in_pass.add(lid)

        title = g(i_title)
        city  = g(i_city)
        state = g(i_state)

        new_leads.append({
            "lead_id": lid,
            "tier": "",
            "first_name": first,
            "last_name": last,
            "full_name": f"{first} {last}",
            "role": title,
            "role_category": "",
            "school_name": company,
            "school_domain": "",
            "city": city,
            "state": state,
            "email": "",
            "email_status": "no_email",
            "linkedin_url": "",
            "linkedin_confidence": "",
            "phone_school": phone_n,
            "phone_direct": "",
            "best_channel": "phone",
            "icp_tier": "",
            "icp_score": "",
            "enriched_at": "",
            "source_page": "from_source_csv",
            "notes": "imported from source CSV — has phone, no email",
        })
        new_phones.append({
            "lead_id": lid,
            "phone_school": phone_n,
            "phone_direct": "",
            "source_page": "from_source_csv",
            "notes": "from source",
        })
        new_emails.append({
            "lead_id": lid,
            "email": "",
            "email_status": "no_email",
            "smtp_code": "",
            "mx_host": "",
        })
        new_lis.append({
            "lead_id": lid,
            "linkedin_url": "",
            "linkedin_confidence": "",
            "source_query": "",
            "snippet": "",
            "engine": "",
        })

    print(f"new leads to import: {len(new_leads)}")
    print(f"  dropped (had email — already handled): {dropped_has_email}")
    print(f"  dropped (no phone): {dropped_no_phone}")
    print(f"  dropped (duplicate within this pass): {dropped_dup_in_pass}")
    print(f"  dropped (duplicate with existing leads): {dropped_dup_with_existing}")

    if not new_leads:
        print("nothing to do")
        return

    # ---- Append to leads_clean.csv ----
    write_header = not LEADS.exists()
    with LEADS.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LEAD_COLS)
        if write_header:
            w.writeheader()
        for r in new_leads:
            w.writerow({k: r.get(k, "") for k in LEAD_COLS})

    # ---- Append to phone_results.csv ----
    PHONE_COLS = ["lead_id", "phone_school", "phone_direct", "source_page", "notes"]
    write_header = not PHONES.exists()
    with PHONES.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=PHONE_COLS)
        if write_header:
            w.writeheader()
        for r in new_phones:
            w.writerow(r)

    # ---- Append to email_verify_results.csv ----
    EMAIL_COLS = ["lead_id", "email", "email_status", "smtp_code", "mx_host"]
    write_header = not EMAILS.exists()
    with EMAILS.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=EMAIL_COLS)
        if write_header:
            w.writeheader()
        for r in new_emails:
            w.writerow(r)

    # ---- Append to linkedin_results.csv ----
    LI_COLS = ["lead_id", "linkedin_url", "linkedin_confidence", "source_query", "snippet", "engine"]
    write_header = not LI.exists()
    with LI.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LI_COLS)
        if write_header:
            w.writeheader()
        for r in new_lis:
            w.writerow(r)

    print(f"✅ imported {len(new_leads)} phone-only leads")
    print(f"   updated: {LEADS.name}, {PHONES.name}, {EMAILS.name}, {LI.name}")
    print(f"   next step: run linkedin_find/_retry on the new lead_ids, then merge + build_xlsx")


if __name__ == "__main__":
    main()
