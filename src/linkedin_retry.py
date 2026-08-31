"""Second-pass LinkedIn discovery for leads we missed the first time.

Targets two groups:
  • leads with no LinkedIn URL at all              -> try fallback queries
  • leads with linkedin_confidence == 'needs_review' -> try fresh queries

Three new query strategies (in order):
  1. "FirstName LastName" "football coach" "STATE" site:linkedin.com
  2. email-as-key   →  "name@domain" site:linkedin.com
  3. email-on-web   →  "name@domain"          (no site filter) — finds the
                                                school staff page that
                                                often LINKS to LinkedIn

Auto-promote rule for needs_review:
  if URL slug contains BOTH first AND last name (as substrings, ≥3 chars each),
  bump confidence to 'medium'.

Reads:   data/linkedin_results.csv  +  data/leads_clean.csv
Writes:  data/linkedin_results.csv  (updates in place, preserves high-conf rows)
"""
from __future__ import annotations

import csv
import os
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from ddgs import DDGS

ROOT = Path(os.environ.get("GRIDIRON_DATASET_ROOT") or Path(__file__).resolve().parent.parent)
LEADS = ROOT / "data" / "leads_clean.csv"
LI = ROOT / "data" / "linkedin_results.csv"

WORKERS = int(os.environ.get("GRIDIRON_LINKEDIN_WORKERS", "5"))
WRITE_EVERY = 10
OUT_FIELDS = ["lead_id", "linkedin_url", "linkedin_confidence", "source_query", "snippet", "engine"]

LINKEDIN_RE = re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/[^\s\"'<>?#]+", re.I)
ENGINES = ("google", "bing", "yahoo")
COACH_KEYWORDS = ("coach", "athletic director", "football", "head coach")


def slug_matches_name(url: str, first: str, last: str) -> bool:
    if not url:
        return False
    slug = url.rsplit("/", 1)[-1].lower()
    f, l = (first or "").lower(), (last or "").lower()
    if l and len(l) >= 3 and l in slug:
        return True
    if f and len(f) >= 3 and f in slug:
        return True
    return False


def slug_matches_both(url: str, first: str, last: str) -> bool:
    """Slug contains BOTH first AND last (strong indicator of right person)."""
    if not url:
        return False
    slug = url.rsplit("/", 1)[-1].lower()
    f, l = (first or "").lower(), (last or "").lower()
    return bool(f and l and len(f) >= 3 and len(l) >= 3 and f in slug and l in slug)


def classify(url: str, snippet: str, lead: dict) -> str:
    if not url:
        return ""
    first, last = lead.get("first_name", ""), lead.get("last_name", "")
    if not slug_matches_name(url, first, last):
        return "needs_review"
    s = (snippet or "").lower()
    school = (lead.get("school_name") or "").lower()
    if school and school in s:
        return "high"
    domain = (lead.get("school_domain") or "").lower()
    if domain and domain in s:
        return "high"
    has_role = any(k in s for k in COACH_KEYWORDS)
    state = (lead.get("state") or "").lower()
    state_present = state and (f", {state}" in s or f" {state} " in s
                               or f"{state}," in s or f"{state}." in s)
    if has_role and state_present:
        return "medium"
    # If slug carries BOTH first+last, that's a strong signal on its own
    if slug_matches_both(url, first, last):
        return "medium"
    return "needs_review"


def search(query: str) -> tuple[list[dict], str]:
    # Brave-only when configured — no DDGS fallback (it wastes time on rate-limited misses).
    if os.environ.get("BRAVE_API_KEY"):
        try:
            from brave_search import brave_search
            return brave_search(query, count=10), "brave"
        except Exception as e:  # noqa: BLE001
            print(f"    brave err: {e.__class__.__name__}", file=sys.stderr)
            return [], ""
    for engine in ENGINES:
        try:
            with DDGS() as ddg:
                results = list(ddg.text(query, backend=engine, max_results=8))
            if results:
                return results, engine
        except Exception as e:  # noqa: BLE001
            print(f"    {engine} err: {e.__class__.__name__}", file=sys.stderr)
            time.sleep(0.6)
    return [], ""


def first_linkedin_hit(results: list[dict], first: str, last: str) -> tuple[str, str]:
    fallback_url, fallback_snippet = "", ""
    for r in results:
        href = r.get("href") or r.get("url") or ""
        body = r.get("body") or ""
        candidates = []
        for src in (href, body):
            for m in LINKEDIN_RE.finditer(src):
                u = m.group(0).split("?")[0].rstrip("/")
                if u not in candidates:
                    candidates.append(u)
        for u in candidates:
            snippet = (r.get("title", "") + " | " + body).strip(" |")
            if slug_matches_name(u, first, last):
                return u, snippet
            if not fallback_url:
                fallback_url, fallback_snippet = u, snippet
    return fallback_url, fallback_snippet


def attempt_for_lead(lead: dict, email_status: str) -> dict | None:
    """Try the 3 new strategies. Return a result dict only if we got an URL."""
    first, last = lead["first_name"], lead["last_name"]
    school = lead["school_name"]
    state = lead.get("state", "")
    email = (lead.get("email") or "").lower()

    queries: list[tuple[str, str]] = []
    if state:
        queries.append(("role_state",
                        f'"{first} {last}" "football coach" "{state}" site:linkedin.com'))
        queries.append(("role_state_alt",
                        f'"{first} {last}" "athletic director" "{state}" site:linkedin.com'))
    if email and email_status in ("valid", "catch_all"):
        queries.append(("email_li", f'"{email}" site:linkedin.com'))
        # email-on-web: find the staff page; its body often contains the LinkedIn URL
        queries.append(("email_web", f'"{email}"'))

    best = {"url": "", "snippet": "", "query": "", "engine": "", "conf": ""}
    rank = {"high": 3, "medium": 2, "needs_review": 1, "": 0}

    for tag, q in queries:
        results, engine = search(q)
        url, snippet = first_linkedin_hit(results, first, last)
        conf = classify(url, snippet, lead)
        if rank[conf] > rank[best["conf"]]:
            best = {"url": url, "snippet": snippet, "query": f"[{tag}] {q}",
                    "engine": engine, "conf": conf}
        if best["conf"] == "high":
            break
        time.sleep(random.uniform(0.7, 1.3))

    if not best["url"]:
        return None
    return {
        "lead_id": lead["lead_id"],
        "linkedin_url": best["url"],
        "linkedin_confidence": best["conf"],
        "source_query": best["query"],
        "snippet": best["snippet"][:300],
        "engine": best["engine"],
    }


def load_email_status() -> dict[str, str]:
    """Return lead_id -> email_status. Empty if file missing."""
    p = ROOT / "data" / "email_verify_results.csv"
    if not p.exists():
        return {}
    with p.open() as f:
        return {r["lead_id"]: r["email_status"] for r in csv.DictReader(f)}


def write_csv(current: dict[str, dict]) -> None:
    tmp = LI.with_suffix(".csv.tmp")
    with tmp.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=OUT_FIELDS)
        w.writeheader()
        for r in current.values():
            w.writerow({k: r.get(k, "") for k in OUT_FIELDS})
    tmp.replace(LI)


def main() -> None:
    # Skip if marker file exists or env var set — user chose not to retry
    skip_marker = ROOT / "SKIP_LINKEDIN_RETRY"
    if skip_marker.exists() or os.environ.get("GRIDIRON_SKIP_LINKEDIN_RETRY") == "1":
        print("skipping linkedin_retry (SKIP_LINKEDIN_RETRY marker or env var set)")
        return

    with LEADS.open() as f:
        leads_by_id = {r["lead_id"]: r for r in csv.DictReader(f)}
    with LI.open() as f:
        current = {r["lead_id"]: r for r in csv.DictReader(f)}
    email_status = load_email_status()

    targets = [lid for lid, r in current.items()
               if not r["linkedin_url"] or r["linkedin_confidence"] == "needs_review"]

    print(f"current LinkedIn rows: {len(current)}")
    print(f"  high       : {sum(1 for r in current.values() if r['linkedin_confidence'] == 'high')}")
    print(f"  medium     : {sum(1 for r in current.values() if r['linkedin_confidence'] == 'medium')}")
    print(f"  needs_review: {sum(1 for r in current.values() if r['linkedin_confidence'] == 'needs_review')}")
    print(f"  empty      : {sum(1 for r in current.values() if not r['linkedin_url'])}")
    print(f"retrying {len(targets)} leads with {WORKERS} parallel workers")
    print()

    rank = {"high": 3, "medium": 2, "needs_review": 1, "": 0}
    promoted_by_slug = 0
    new_high = 0
    new_medium = 0
    new_nr = 0
    upgraded = 0
    lock = threading.Lock()
    updates_since_flush = 0

    # ---- Auto-promote pass (no network, fast) ----
    search_targets = []
    for lid in targets:
        lead = leads_by_id.get(lid, {})
        if not lead:
            continue
        cur = current[lid]
        cur_url = cur["linkedin_url"]
        if cur_url and cur["linkedin_confidence"] == "needs_review" and slug_matches_both(
                cur_url, lead.get("first_name", ""), lead.get("last_name", "")):
            cur["linkedin_confidence"] = "medium"
            cur["source_query"] = cur["source_query"] + " [promoted: slug=first+last]"
            promoted_by_slug += 1
            upgraded += 1
            continue
        search_targets.append(lid)

    if promoted_by_slug:
        print(f"auto-promoted {promoted_by_slug} via slug match (no search needed)")
        write_csv(current)
    print(f"running parallel search on {len(search_targets)} remaining leads")
    print()

    def process_lead(lid: str) -> tuple[str, dict | None, str]:
        lead = leads_by_id.get(lid, {})
        if not lead:
            return lid, None, ""
        em_status = email_status.get(lid, "")
        result = attempt_for_lead(lead, em_status)
        time.sleep(random.uniform(0.3, 0.6))
        return lid, result, current[lid]["linkedin_confidence"]

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(process_lead, lid): lid for lid in search_targets}
        done = 0
        for fut in as_completed(futures):
            lid = futures[fut]
            try:
                _lid, result, prev_conf = fut.result()
            except Exception as e:  # noqa: BLE001
                result, prev_conf = None, current[lid]["linkedin_confidence"]
                print(f"  err {lid}: {e.__class__.__name__}", file=sys.stderr)
            done += 1
            lead = leads_by_id.get(lid, {})
            with lock:
                if result and rank[result["linkedin_confidence"]] > rank[prev_conf]:
                    current[lid] = result
                    if result["linkedin_confidence"] == "high": new_high += 1
                    elif result["linkedin_confidence"] == "medium": new_medium += 1
                    else: new_nr += 1
                    if prev_conf == "needs_review" and result["linkedin_confidence"] in ("high", "medium"):
                        upgraded += 1
                    url_short = result["linkedin_url"].replace("https://www.linkedin.com/in/", "li:")
                    tag = result["source_query"].split("]")[0].lstrip("[") if "]" in result["source_query"] else ""
                    print(f"[{done:3d}/{len(search_targets)}] {lead.get('first_name',''):>10s} {lead.get('last_name',''):<12s}  {result['linkedin_confidence']:12s} via {tag:14s} {url_short}", flush=True)
                else:
                    print(f"[{done:3d}/{len(search_targets)}] {lead.get('first_name',''):>10s} {lead.get('last_name',''):<12s}  no improvement", flush=True)
                updates_since_flush += 1
                if updates_since_flush >= WRITE_EVERY:
                    write_csv(current)
                    updates_since_flush = 0

    write_csv(current)
    print()
    print("=== summary ===")
    print(f"  auto-promoted (slug=first+last): {promoted_by_slug}")
    print(f"  new high from re-search        : {new_high}")
    print(f"  new medium from re-search      : {new_medium}")
    print(f"  new needs_review from re-search: {new_nr}")
    print(f"  total upgraded to usable       : {upgraded}")


if __name__ == "__main__":
    main()
