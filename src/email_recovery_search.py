"""Find emails for leads via Google search snippets.

Different from domain_recovery.py (which guesses firstname.lastname@domain).
This script searches Google for the coach's name + school + email keyword,
then extracts email addresses that appear near the coach's name in result
snippets — same approach as phone_recovery but for emails.

Targets: leads whose email_status is NOT 'valid' or 'catch_all'.
After finding a candidate email, SMTP-probes to verify.

Resumable: skips lead_ids already in data/email_recovery_search.csv.

Env:
  BRAVE_API_KEY (optional) — uses Brave if set, else DDGS (free)
  GRIDIRON_EMAIL_SEARCH_WORKERS = 5
  GRIDIRON_EMAIL_SEARCH_TIER = "ALL" (default — process every emailless lead)
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
EMAIL_VERIFY = ROOT / "data" / "email_verify_results.csv"
OUT = ROOT / "data" / "email_recovery_search.csv"

OUT_FIELDS = ["lead_id", "new_email", "verify_status", "source_query", "snippet"]
WORKERS = int(os.environ.get("GRIDIRON_EMAIL_SEARCH_WORKERS", "5"))
TIER_FILTER = os.environ.get("GRIDIRON_EMAIL_SEARCH_TIER", "ALL").upper()

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
JUNK_DOMAINS = {
    "example.com", "domain.com", "yourdomain.com", "test.com",
    "email.com", "school.com", "company.com", "gmail.example",
}
# Generic role-based locals that aren't personal emails — skip these.
SHARED_LOCALS = {
    "info", "contact", "athletics", "athletic", "sports", "office",
    "admin", "administration", "admissions", "support", "help",
    "team", "staff", "general", "main", "media", "news", "press",
    "webmaster", "noreply", "no-reply", "do-not-reply",
    "coach", "coaches", "football", "ad", "athleticdirector",
    "principal", "school", "communications", "marketing",
    "alumni", "boosters", "tickets", "donate", "events",
    "feedback", "inquiries", "questions", "service", "services",
}


def is_personal_email(email: str, first: str, last: str) -> bool:
    """Reject shared/generic emails; prefer ones tied to the person's name."""
    if "@" not in email:
        return False
    local = email.split("@", 1)[0].lower()
    # Reject generic locals
    if local in SHARED_LOCALS:
        return False
    # Reject locals that are too short (less than 3 chars often generic)
    if len(local) < 3:
        return False
    # Strong signal: email contains first or last name
    f, l = first.lower(), last.lower()
    if (f and len(f) >= 3 and f in local) or (l and len(l) >= 3 and l in local):
        return True
    # Allow other locals (e.g., initials + last name) — slightly weaker but personal
    # Reject if local is purely a role keyword
    role_words = {"head", "assist", "assistant", "director", "manager",
                  "coordinator", "trainer"}
    if local in role_words:
        return False
    return True

# Reuse SMTP probe from domain_recovery
sys.path.insert(0, str(Path(__file__).parent))
from domain_recovery import smtp_probe


def find_email_near_name(text: str, full_name: str) -> str:
    """Return PERSONAL email near the coach's name (rejects shared/generic ones)."""
    name_lc = full_name.lower().strip()
    if not name_lc:
        return ""
    text_lc = text.lower()
    name_pos = text_lc.find(name_lc)
    if name_pos < 0:
        last = full_name.split()[-1].lower() if full_name.split() else ""
        if last and len(last) >= 4:
            name_pos = text_lc.find(last)
        if name_pos < 0:
            return ""
    win_start = max(0, name_pos - 300)
    win_end = min(len(text), name_pos + 300)
    window = text[win_start:win_end]
    first_part = full_name.split()[0] if full_name.split() else ""
    last_part = full_name.split()[-1] if full_name.split() else ""
    # First pass: emails whose local-part contains first or last name (strongest signal)
    name_matches = []
    other_matches = []
    for m in EMAIL_RE.finditer(window):
        email = m.group(0).lower()
        dom = email.rsplit("@", 1)[-1]
        if dom in JUNK_DOMAINS:
            continue
        if not is_personal_email(email, first_part, last_part):
            continue
        local = email.split("@", 1)[0]
        if (first_part and len(first_part) >= 3 and first_part.lower() in local) \
                or (last_part and len(last_part) >= 3 and last_part.lower() in local):
            name_matches.append(email)
        else:
            other_matches.append(email)
    if name_matches:
        return name_matches[0]
    if other_matches:
        return other_matches[0]
    return ""


def do_search(q: str) -> list[dict]:
    if os.environ.get("BRAVE_API_KEY"):
        try:
            from brave_search import brave_search
            return brave_search(q, count=8)
        except Exception:  # noqa: BLE001
            return []
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


def search_for_email(lead: dict) -> dict:
    first = (lead.get("first_name") or "").strip()
    last = (lead.get("last_name") or "").strip()
    school = (lead.get("school_name") or "").strip()
    full_name = f"{first} {last}"

    result = {
        "lead_id": lead["lead_id"],
        "new_email": "",
        "verify_status": "",
        "source_query": "",
        "snippet": "",
    }
    if not (first and last and school):
        return result

    queries = [
        f'"{full_name}" "{school}" email',
        f'"{full_name}" "{school}" contact',
        f'"{full_name}" "{school}" "@"',
    ]
    for q in queries:
        results = do_search(q)
        for r in results:
            blob = (r.get("title", "") + " | " + (r.get("body") or "")).strip(" |")
            email = find_email_near_name(blob, full_name)
            if email:
                # Verify with SMTP
                try:
                    status, _code, _msg = smtp_probe(email)
                except Exception:  # noqa: BLE001
                    status = "unknown"
                if status in ("valid", "catch_all", "unknown"):
                    result.update({
                        "new_email": email,
                        "verify_status": status,
                        "source_query": q,
                        "snippet": blob[:300],
                    })
                    return result
        time.sleep(0.05)
    return result


def main() -> None:
    with LEADS.open() as f:
        leads = list(csv.DictReader(f))
    verify = {}
    if EMAIL_VERIFY.exists():
        with EMAIL_VERIFY.open() as f:
            verify = {r["lead_id"]: r for r in csv.DictReader(f)}

    # Target: leads whose current email_status is NOT valid/catch_all
    targets = []
    for lead in leads:
        v = verify.get(lead["lead_id"])
        if v and v.get("email_status") in ("valid", "catch_all"):
            continue
        if not (lead.get("first_name") and lead.get("last_name") and lead.get("school_name")):
            continue
        targets.append(lead)

    print(f"leads total: {len(leads)}")
    print(f"targets (no valid email yet): {len(targets)}")
    print(f"workers: {WORKERS}, mode: {'Brave' if os.environ.get('BRAVE_API_KEY') else 'DDGS (free)'}")
    print()

    done = set()
    if OUT.exists():
        with OUT.open() as f:
            done = {r["lead_id"] for r in csv.DictReader(f)}
    todo = [l for l in targets if l["lead_id"] not in done]
    print(f"already done: {len(done)}, todo: {len(todo)}")
    print()

    is_new = not OUT.exists()
    lock = threading.Lock()
    found = 0
    with OUT.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=OUT_FIELDS)
        if is_new:
            w.writeheader()
            f.flush()
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            futures = {ex.submit(search_for_email, l): l for l in todo}
            completed = 0
            for fut in as_completed(futures):
                lead = futures[fut]
                try:
                    result = fut.result()
                except Exception as e:  # noqa: BLE001
                    result = {"lead_id": lead["lead_id"], "new_email":"", "verify_status":"",
                              "source_query": f"err:{e.__class__.__name__}", "snippet":""}
                with lock:
                    completed += 1
                    w.writerow(result)
                    f.flush()
                    if result["new_email"]:
                        found += 1
                    tag = f'{result["new_email"]} ({result["verify_status"]})' if result["new_email"] else "miss"
                    fn = f'{lead.get("first_name","")} {lead.get("last_name","")}'.strip()
                    print(f"[{completed:5d}/{len(todo)}] {fn[:24]:24s} @ {lead.get('school_name','')[:28]:28s}  {tag}", flush=True)

    print()
    print(f"=== summary ===")
    print(f"  processed: {completed}, new emails found: {found}")
    hit = found / max(completed, 1) * 100
    print(f"  hit rate: {hit:.1f}%")


if __name__ == "__main__":
    main()
