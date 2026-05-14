"""Merge enrichment outputs into a single canonical leads table.

Reads:
    data/leads_clean.csv
    data/linkedin_results.csv
    data/phone_results.csv
    data/email_verify_results.csv

Writes:
    data/enriched.csv  — canonical merged dataset (one row per lead)
"""
from __future__ import annotations

import csv
import os
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get("GRIDIRON_DATASET_ROOT") or Path(__file__).resolve().parent.parent)
DATA = ROOT / "data"


def index_by_lead_id(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    with path.open() as f:
        return {r["lead_id"]: r for r in csv.DictReader(f)}


def assign_tier(row: dict) -> str:
    has_li = bool(row.get("linkedin_url")) and row.get("linkedin_confidence") in ("high", "medium")
    has_phone = bool(row.get("phone_school") or row.get("phone_direct"))
    # 'unknown' here is treated as usable only if it came from a recovered domain
    # (the merge step sets it on recovery + adds a note). Standalone 'unknown' stays as not-ok.
    email_ok = row.get("email_status") in ("valid", "catch_all") or (
        row.get("email_status") == "unknown" and "recovered domain" in (row.get("notes") or "")
    )

    if has_li and email_ok:
        return "A"
    if has_phone and email_ok:
        return "B"
    if email_ok:
        return "C"
    if has_li or has_phone:
        return "D"
    return "E"


def assign_best_channel(row: dict) -> str:
    if row.get("linkedin_url") and row.get("linkedin_confidence") in ("high", "medium"):
        return "linkedin_dm"
    if row.get("phone_school") or row.get("phone_direct"):
        return "call"
    if row.get("email_status") in ("valid", "catch_all"):
        return "email"
    return "low_signal"


def main() -> None:
    leads_path = DATA / "leads_clean.csv"
    linkedin = index_by_lead_id(DATA / "linkedin_results.csv")
    phones = index_by_lead_id(DATA / "phone_results.csv")
    emails = index_by_lead_id(DATA / "email_verify_results.csv")
    recovery = index_by_lead_id(DATA / "domain_recovery.csv")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows = []
    with leads_path.open() as f:
        for r in csv.DictReader(f):
            lid = r["lead_id"]
            li = linkedin.get(lid, {})
            ph = phones.get(lid, {})
            em = emails.get(lid, {})

            r["linkedin_url"] = li.get("linkedin_url", "") or ""
            r["linkedin_confidence"] = li.get("linkedin_confidence", "") or ""
            # phone_school: prefer scrape, fall back to source MaxPreps phone
            scraped_school = (ph.get("phone_school") or "").strip()
            if scraped_school:
                r["phone_school"] = scraped_school
            # keep existing r["phone_school"] if no scrape (already populated from MaxPreps)
            r["phone_direct"] = (ph.get("phone_direct") or "").strip()
            # source_page: where we found the school phone
            r["source_page"] = ph.get("source_page", "") or ""

            r["email_status"] = em.get("email_status", "") or ""

            # Apply high-confidence domain recovery if it exists
            rec = recovery.get(lid, {})
            rec_conf = rec.get("recovery_confidence", "")
            if rec_conf == "high" and rec.get("new_email") and rec.get("new_status"):
                r["email"] = rec["new_email"]
                r["email_status"] = rec["new_status"]
                # remember what we replaced for transparency in the dashboard
                r.setdefault("notes", "")
                r["notes"] = f"email recovered from dead domain ({rec.get('original_email','')}) → {rec['new_email']}"

            # notes: pile up anything interesting
            notes = [r.get("notes")] if r.get("notes") else []
            if li.get("linkedin_confidence") == "needs_review" and li.get("linkedin_url"):
                notes.append("linkedin needs review")
            if r["email_status"] == "catch_all":
                notes.append("email is catch-all (could still bounce)")
            if r["email_status"] == "invalid":
                notes.append("email rejected by mail server")
            if r["email_status"] == "unknown" and rec_conf == "high":
                notes.append("recovered domain — SMTP probe inconclusive, likely working")
            if ph.get("notes"):
                notes.append(ph["notes"])
            r["notes"] = "; ".join(n for n in notes if n)

            r["best_channel"] = assign_best_channel(r)
            r["tier"] = assign_tier(r)
            r["enriched_at"] = now
            rows.append(r)

    out_path = DATA / "enriched.csv"
    fieldnames = list(rows[0].keys()) if rows else []
    with out_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)

    # summary
    from collections import Counter
    tiers = Counter(r["tier"] for r in rows)
    statuses = Counter(r["email_status"] or "missing" for r in rows)
    li_conf = Counter(r["linkedin_confidence"] or "no_url" for r in rows)
    phones_found = sum(1 for r in rows if r["phone_school"] or r["phone_direct"])

    print(f"merged {len(rows)} leads -> {out_path}")
    print(f"  tiers: {dict(tiers)}")
    print(f"  email status: {dict(statuses)}")
    print(f"  linkedin confidence: {dict(li_conf)}")
    print(f"  with any phone: {phones_found}")


if __name__ == "__main__":
    main()
