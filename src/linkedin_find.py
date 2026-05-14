"""Find LinkedIn profile URLs for each lead via DuckDuckGo dorking.

Strategy: search for the person constrained to linkedin.com/in/... URLs.
Validate that the school name (or domain, or state+role) appears in the
result snippet so we don't grab the wrong John Smith.

Outputs data/linkedin_results.csv with columns:
    lead_id, linkedin_url, linkedin_confidence, source_query, snippet
Resumable: skips lead_ids already present in the output file.
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
OUT = ROOT / "data" / "linkedin_results.csv"

OUT_FIELDS = ["lead_id", "linkedin_url", "linkedin_confidence", "source_query", "snippet", "engine"]
WORKERS = int(os.environ.get("GRIDIRON_LINKEDIN_FIND_WORKERS", "5"))

LINKEDIN_RE = re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/[^\s\"'<>?#]+", re.I)
COACH_KEYWORDS = ("coach", "athletic director", "football", "head coach")
ENGINES = ("google", "bing", "yahoo")


def load_done(path: Path) -> set[str]:
    if not path.exists():
        return set()
    done = set()
    with path.open() as f:
        r = csv.DictReader(f)
        for row in r:
            done.add(row["lead_id"])
    return done


def classify(url: str, snippet: str, lead: dict) -> str:
    """Three-tier confidence with strict name gate.

    high   — name in slug AND (school OR domain) appears in snippet
    medium — name in slug AND state code appears in snippet (with role keyword)
    needs_review — anything weaker (likely wrong person)
    ""     — no URL at all
    """
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
    state_present = state and (f", {state}" in s or f" {state} " in s or f"{state}," in s
                               or f"{state}\n" in s or f"{state}." in s)
    if has_role and state_present:
        return "medium"
    return "needs_review"


def slug_matches_name(url: str, first: str, last: str) -> bool:
    """LinkedIn slug should contain the lead's first or last name."""
    if not url:
        return False
    slug = url.rsplit("/", 1)[-1].lower()
    f, l = (first or "").lower(), (last or "").lower()
    if l and len(l) >= 3 and l in slug:
        return True
    if f and len(f) >= 3 and f in slug:
        return True
    return False


def first_linkedin_hit(results: list[dict], first: str, last: str) -> tuple[str, str]:
    """Return (url, snippet) for the best linkedin.com/in/ result.

    Strategy: pick the first URL whose slug contains the lead's first or last
    name. If none match, fall back to the first URL but flag it as low-quality
    via the empty-snippet sentinel — the classifier will downgrade it.
    """
    fallback_url, fallback_snippet = "", ""
    for r in results:
        href = r.get("href") or r.get("url") or ""
        body = r.get("body") or ""
        # Collect all linkedin URLs from this result (some engines glom many into one row)
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


def search(query: str) -> tuple[list[dict], str]:
    """Rotate through Google → Bing → Yahoo. Returns (results, engine_used).

    Some engines fail open ("No results found"), some throw on rate-limit.
    We treat both as "try the next engine".
    """
    for engine in ENGINES:
        try:
            with DDGS() as ddg:
                results = list(ddg.text(query, backend=engine, max_results=8))
            if results:
                return results, engine
        except Exception as e:  # noqa: BLE001
            print(f"  {engine} err: {e.__class__.__name__}: {str(e)[:80]}", file=sys.stderr)
            time.sleep(1.0)
    return [], ""


def find_for_lead(lead: dict) -> dict:
    first, last = lead["first_name"], lead["last_name"]
    school = lead["school_name"]
    domain = lead.get("school_domain", "")
    state = lead.get("state", "")

    # Single primary query — fallback queries return too many wrong-person hits
    # via SERP-aggregation to be worth the 3x time cost.
    q = f'"{first} {last}" "{school}" site:linkedin.com/in'
    results, engine = search(q)
    url, snippet = first_linkedin_hit(results, first, last)
    conf = classify(url, snippet, lead)

    # If the primary missed entirely AND we have a domain, one cheap fallback
    if not url and domain:
        q2 = f'"{first} {last}" "{domain}" site:linkedin.com/in'
        time.sleep(0.5)
        results, engine = search(q2)
        url, snippet = first_linkedin_hit(results, first, last)
        conf = classify(url, snippet, lead)
        if url:
            q = q2

    return {
        "lead_id": lead["lead_id"],
        "linkedin_url": url,
        "linkedin_confidence": conf,
        "source_query": q,
        "snippet": snippet[:300],
        "engine": engine,
    }


def main() -> None:
    with LEADS.open() as f:
        leads = list(csv.DictReader(f))
    done = load_done(OUT)
    todo = [l for l in leads if l["lead_id"] not in done]
    print(f"leads total: {len(leads)} | already done: {len(done)} | todo: {len(todo)} | workers: {WORKERS}")

    is_new_file = not OUT.exists()
    lock = threading.Lock()

    def process(lead: dict) -> dict:
        try:
            r = find_for_lead(lead)
        except Exception as e:  # noqa: BLE001
            r = {"lead_id": lead["lead_id"], "linkedin_url":"", "linkedin_confidence":"",
                 "source_query": f"err:{e.__class__.__name__}", "snippet":"", "engine":""}
        time.sleep(random.uniform(0.4, 0.8))
        return r

    with OUT.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=OUT_FIELDS)
        if is_new_file:
            w.writeheader()
        completed = 0
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            futures = {ex.submit(process, l): l for l in todo}
            for fut in as_completed(futures):
                lead = futures[fut]
                try:
                    result = fut.result()
                except Exception as e:  # noqa: BLE001
                    result = {"lead_id": lead["lead_id"], "linkedin_url":"", "linkedin_confidence":"",
                              "source_query": f"err:{e.__class__.__name__}", "snippet":"", "engine":""}
                with lock:
                    completed += 1
                    w.writerow(result)
                    f.flush()
                    conf = result["linkedin_confidence"] or "miss"
                    url_short = result["linkedin_url"].replace("https://www.linkedin.com/in/", "li:") or "-"
                    eng = result["engine"] or "-"
                    print(f"[{completed:3d}/{len(todo)}] {lead['first_name']:12s} {lead['last_name']:14s} @ {lead['school_name'][:36]:36s}  {conf:12s} {eng:8s} {url_short}", flush=True)


if __name__ == "__main__":
    main()
