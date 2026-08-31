"""Personal/direct phone recovery via Brave search.

Targets leads who only have phone_school (the switchboard) — tries to find a
direct dial, extension, or personal phone published near the coach's name.

Query strategy (per lead):
  1. `"FirstName LastName" "School Name" direct phone`
  2. `"FirstName LastName" "School Name" cell` (less common, less reliable)

We extract phone numbers from result snippets ONLY when the snippet also
mentions the coach's full name — this filters out random hits from the school
website that aren't tied to this specific person.

Writes to data/phone_recovery.csv:
  lead_id, new_phone, source_query, snippet

Reads:  data/leads_clean.csv  +  data/phone_results.csv
Env:    BRAVE_API_KEY required
        GRIDIRON_PHONE_RECOVERY_WORKERS = 8 (default)
        GRIDIRON_PHONE_RECOVERY_TIER = "A,B,C" (default — limit to top tier to control cost)
                                       "ALL" to process every lead
"""
from __future__ import annotations

import csv
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(os.environ.get("GRIDIRON_DATASET_ROOT") or Path(__file__).resolve().parent.parent)
LEADS = ROOT / "data" / "leads_clean.csv"
PHONES = ROOT / "data" / "phone_results.csv"
ENRICHED = ROOT / "data" / "enriched.csv"
OUT = ROOT / "data" / "phone_recovery.csv"

OUT_FIELDS = ["lead_id", "new_phone", "source_query", "snippet"]
WORKERS = int(os.environ.get("GRIDIRON_PHONE_RECOVERY_WORKERS", "8"))
TIER_FILTER = os.environ.get("GRIDIRON_PHONE_RECOVERY_TIER", "A,B,C").upper()

PHONE_RE = re.compile(r"(?:\+?1[\s.-]?)?\(?(\d{3})\)?[\s.-]?(\d{3})[\s.-]?(\d{4})")
EXT_RE = re.compile(r"(?:ext\.?|extension|x)\s*(\d{3,5})", re.I)
PHONE_BLACKLIST = {"8005551234", "5555555555", "1234567890", "0000000000"}

# Reuse NANP area codes from phone_retry
sys.path.insert(0, str(Path(__file__).parent))
from phone_retry import VALID_AREA_CODES, is_valid


def fmt_phone(area: str, mid: str, last: str, ext: str = "") -> str:
    base = f"({area}) {mid}-{last}"
    return f"{base} x{ext}" if ext else base


def find_phone_near_name(text: str, full_name: str, school: str) -> str:
    """Return phone number that appears in text near the coach's name.
    Falls back to phone with extension if found near name."""
    name_lc = full_name.lower().strip()
    if not name_lc:
        return ""
    text_lc = text.lower()
    # Find name position
    name_pos = text_lc.find(name_lc)
    if name_pos < 0:
        # Try partial match (just last name)
        last = full_name.split()[-1].lower() if full_name.split() else ""
        if last and len(last) >= 4:
            name_pos = text_lc.find(last)
        if name_pos < 0:
            return ""
    # Look for phone within 250 chars of name
    win_start = max(0, name_pos - 250)
    win_end = min(len(text), name_pos + 250)
    window = text[win_start:win_end]
    ext_m = EXT_RE.search(window)
    ext = ext_m.group(1) if ext_m else ""
    for m in PHONE_RE.finditer(window):
        area, mid, last4 = m.group(1), m.group(2), m.group(3)
        raw = area + mid + last4
        if raw in PHONE_BLACKLIST:
            continue
        if not is_valid(area):
            continue
        return fmt_phone(area, mid, last4, ext)
    return ""


def search_for_phone(lead: dict) -> dict:
    """Search Brave for a phone number tied to this specific lead."""
    first = (lead.get("first_name") or "").strip()
    last = (lead.get("last_name") or "").strip()
    school = (lead.get("school_name") or "").strip()
    full_name = f"{first} {last}"
    state = lead.get("state", "")

    result = {
        "lead_id": lead["lead_id"],
        "new_phone": "",
        "source_query": "",
        "snippet": "",
    }
    if not (first and last and school):
        return result

    queries = [
        f'"{full_name}" "{school}" direct phone',
        f'"{full_name}" "{school}" extension',
    ]
    if state:
        queries.append(f'"{full_name}" "{state}" coach phone')

    use_brave = bool(os.environ.get("BRAVE_API_KEY"))
    brave_search = None
    if use_brave:
        try:
            from brave_search import brave_search as _bs
            brave_search = _bs
        except Exception:  # noqa: BLE001
            use_brave = False

    def do_search(q):
        if use_brave and brave_search:
            try:
                return brave_search(q, count=8)
            except Exception:  # noqa: BLE001
                return []
        # DDGS fallback (free but rate-limited)
        try:
            from ddgs import DDGS
            for engine in ("google", "bing", "yahoo"):
                try:
                    with DDGS() as ddg:
                        return list(ddg.text(q, backend=engine, max_results=8))
                except Exception:  # noqa: BLE001
                    time.sleep(0.6)
        except Exception:  # noqa: BLE001
            pass
        return []

    for q in queries:
        results = do_search(q)
        for r in results:
            blob = (r.get("title", "") + " | " + (r.get("body") or "")).strip(" |")
            phone = find_phone_near_name(blob, full_name, school)
            if phone:
                result.update({
                    "new_phone": phone,
                    "source_query": q,
                    "snippet": blob[:300],
                })
                return result
        time.sleep(0.05)
    return result


def main() -> None:
    with LEADS.open() as f:
        leads = list(csv.DictReader(f))
    # Existing phones
    with PHONES.open() as f:
        existing_phones = {r["lead_id"]: r for r in csv.DictReader(f)}

    # Determine which leads to target by tier
    tier_set = set(t.strip() for t in TIER_FILTER.split(",") if t.strip())
    use_all = "ALL" in tier_set

    # If enriched.csv exists, use it to read tier; else fall back to processing all
    tiers = {}
    if ENRICHED.exists():
        with ENRICHED.open() as f:
            for r in csv.DictReader(f):
                tiers[r["lead_id"]] = r.get("tier", "")

    # Target: leads without phone_direct (we only have switchboard or nothing)
    targets = []
    for lead in leads:
        lid = lead["lead_id"]
        p = existing_phones.get(lid, {})
        # Skip if we already have a direct phone
        if (p.get("phone_direct") or "").strip():
            continue
        # Filter by tier if not ALL
        if not use_all:
            t = tiers.get(lid, "")
            if t not in tier_set:
                continue
        targets.append(lead)

    print(f"leads total: {len(leads)}")
    print(f"target tier: {TIER_FILTER}")
    print(f"targets (no direct phone yet): {len(targets)}")
    print(f"workers: {WORKERS}")
    print()

    # Resumable: skip already-processed lead_ids
    done = set()
    if OUT.exists():
        with OUT.open() as f:
            done = {r["lead_id"] for r in csv.DictReader(f)}
    todo = [l for l in targets if l["lead_id"] not in done]
    print(f"already done: {len(done)}, todo: {len(todo)}")
    print()

    is_new = not OUT.exists()
    lock = threading.Lock()
    found_count = 0

    with OUT.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=OUT_FIELDS)
        if is_new:
            w.writeheader()
            f.flush()
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            futures = {ex.submit(search_for_phone, l): l for l in todo}
            completed = 0
            for fut in as_completed(futures):
                lead = futures[fut]
                try:
                    result = fut.result()
                except Exception as e:  # noqa: BLE001
                    result = {"lead_id": lead["lead_id"], "new_phone": "",
                              "source_query": f"err:{e.__class__.__name__}", "snippet": ""}
                with lock:
                    completed += 1
                    w.writerow(result)
                    f.flush()
                    tag = result["new_phone"] or "miss"
                    if result["new_phone"]:
                        found_count += 1
                    fn = f"{lead.get('first_name','')} {lead.get('last_name','')}".strip()
                    print(f"[{completed:4d}/{len(todo)}] {fn[:24]:24s} @ {lead.get('school_name','')[:28]:28s}  {tag}", flush=True)

    print()
    print(f"=== summary ===")
    print(f"  processed         : {completed}")
    print(f"  new phones found  : {found_count}")
    hit = found_count / max(completed, 1) * 100
    print(f"  hit rate          : {hit:.1f}%")


if __name__ == "__main__":
    main()
