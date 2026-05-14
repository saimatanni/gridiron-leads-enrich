"""Post-filter domain recovery results to drop false positives.

The recovery script's gate (school-distinctive-token in domain) catches cases
like 'warbyparker.com' for a school named 'Parker' or 'silvertoncasino.com' for
'Silverton'. We can't trust those — the marketing team would mail unrelated
businesses.

Filter rule: the recovered domain must look like an educational/school domain:
   - contains 'school', 'schools', 'highschool', 'isd', 'k12', 'academy',
     'prep', 'catholic', 'district', '.edu'
   - OR matches a known-school-domain pattern

We add a 'recovery_confidence' column:
   high   -> domain has a clear school keyword; auto-merge into output
   review -> domain looks ambiguous; show in dashboard but don't auto-apply
"""
from __future__ import annotations

import csv
import os
import re
from pathlib import Path

ROOT = Path(os.environ.get("GRIDIRON_DATASET_ROOT") or Path(__file__).resolve().parent.parent)
SRC = ROOT / "data" / "domain_recovery.csv"
OUT = ROOT / "data" / "domain_recovery.csv"

SCHOOL_KEYWORDS = (
    "school", "schools", "highschool", "high-school", "isd",
    "k12", "academy", "prep", "catholic", "district", "education",
    "preparatory", "diocese", "diocesan", ".edu",
)

# A small allowlist of well-known prep/private school domains where the
# domain doesn't contain a school keyword but is the right school.
# These come from manual review of the recovery output.
KNOWN_GOOD = {
    "jserra.org",
    "saint-edward.org",
    "dematha.org",
    "bishopgorman.org",
    "stignatius.org",
}


def is_school_domain(domain: str) -> bool:
    d = (domain or "").lower()
    if not d:
        return False
    if d in KNOWN_GOOD:
        return True
    if any(kw in d for kw in SCHOOL_KEYWORDS):
        return True
    return False


def main() -> None:
    if not SRC.exists():
        print(f"no file at {SRC}")
        return
    with SRC.open() as f:
        rows = list(csv.DictReader(f))
    fields = list(rows[0].keys()) if rows else []
    if "recovery_confidence" not in fields:
        fields.append("recovery_confidence")

    counts = {"high": 0, "review": 0, "miss": 0}
    for r in rows:
        if not r.get("new_domain"):
            r["recovery_confidence"] = ""
            counts["miss"] += 1
            continue
        if is_school_domain(r["new_domain"]):
            r["recovery_confidence"] = "high"
            counts["high"] += 1
        else:
            r["recovery_confidence"] = "review"
            counts["review"] += 1

    with OUT.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    print(f"rows: {len(rows)}")
    print(f"  high   (real school domain — auto-apply): {counts['high']}")
    print(f"  review (looks like wrong domain — flag):  {counts['review']}")
    print(f"  miss   (no domain found):                 {counts['miss']}")

    # Show the high-confidence recoveries
    print()
    print("=== High-confidence recoveries (will be merged) ===")
    for r in rows:
        if r["recovery_confidence"] == "high":
            print(f"  {r['original_email']:50s}  ->  {r['new_email']:50s}  ({r['new_status']})")

    print()
    print("=== Review-needed (NOT auto-applied — domain doesn't look like a school) ===")
    for r in rows:
        if r["recovery_confidence"] == "review":
            print(f"  {r['original_email']:50s}  ->  {r['new_email']:50s}  ({r['new_status']})")


if __name__ == "__main__":
    main()
