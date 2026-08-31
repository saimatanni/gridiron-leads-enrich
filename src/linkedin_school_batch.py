"""School-batched LinkedIn discovery.

Instead of one Google search per lead (slow), do ONE search per unique
school: `site:linkedin.com "School Name" coach`. That single query returns
5-10 LinkedIn profiles of staff at that school. Match each result by slug
against the leads we have at that school.

Use this when:
  - You have many leads sharing a small number of schools (10x compression).
  - You want LinkedIn URLs for leads imported via import_phone_only.py
    (i.e. emailless rows that already have a phone).

Reads:  data/leads_clean.csv
Updates: data/linkedin_results.csv  (only fills empty linkedin_url rows)

Env vars:
  GRIDIRON_LI_BATCH_WORKERS = 5
  GRIDIRON_LI_BATCH_ONLY_EMPTY = 1  (default: only enrich leads with empty URL)
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

WORKERS = int(os.environ.get("GRIDIRON_LI_BATCH_WORKERS", "5"))
ONLY_EMPTY = os.environ.get("GRIDIRON_LI_BATCH_ONLY_EMPTY", "1") == "1"

LINKEDIN_RE = re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/[^\s\"'<>?#]+", re.I)
ENGINES = ("google", "bing", "yahoo")
LI_COLS = ["lead_id", "linkedin_url", "linkedin_confidence", "source_query", "snippet", "engine"]
COACH_KEYWORDS = ("coach", "athletic director", "football", "head coach", "assistant")


def slug_of(url: str) -> str:
    return url.rsplit("/", 1)[-1].lower() if url else ""


def slug_matches(slug: str, first: str, last: str) -> tuple[bool, bool]:
    """Returns (matches_either, matches_both). For confidence rating."""
    f, l = first.lower(), last.lower()
    has_f = bool(f) and len(f) >= 3 and f in slug
    has_l = bool(l) and len(l) >= 3 and l in slug
    return (has_f or has_l), (has_f and has_l)


def search(query: str) -> tuple[list[dict], str]:
    if os.environ.get("BRAVE_API_KEY"):
        try:
            from brave_search import brave_search
            results = brave_search(query, count=15)
            if results:
                return results, "brave"
        except Exception as e:  # noqa: BLE001
            print(f"  brave err: {e.__class__.__name__}", file=sys.stderr)
    for engine in ENGINES:
        try:
            with DDGS() as ddg:
                results = list(ddg.text(query, backend=engine, max_results=12))
            if results:
                return results, engine
        except Exception as e:  # noqa: BLE001
            print(f"  {engine} err: {e.__class__.__name__}", file=sys.stderr)
            time.sleep(0.6)
    return [], ""


def collect_linkedin_hits(results: list[dict]) -> list[tuple[str, str]]:
    """Returns list of (url, snippet). Dedupes URLs."""
    seen = set()
    hits = []
    for r in results:
        body = r.get("body") or ""
        href = r.get("href") or r.get("url") or ""
        title = r.get("title") or ""
        for src in (href, body):
            for m in LINKEDIN_RE.finditer(src):
                u = m.group(0).split("?")[0].rstrip("/").lower()
                if u not in seen:
                    seen.add(u)
                    hits.append((u, f"{title} | {body}".strip(" |")))
    return hits


def search_school(school: str, state: str = "") -> tuple[list[tuple[str, str]], str, str]:
    """One Google search for the school's coaches on LinkedIn.
    Returns (hits, engine_used, query_used)."""
    if state:
        q = f'site:linkedin.com "{school}" "{state}" coach'
    else:
        q = f'site:linkedin.com "{school}" coach'
    results, engine = search(q)
    hits = collect_linkedin_hits(results)
    # Fallback: drop the "coach" keyword
    if not hits:
        q2 = f'site:linkedin.com "{school}"'
        results, engine = search(q2)
        hits = collect_linkedin_hits(results)
        if hits:
            q = q2
    return hits, engine, q


def main() -> None:
    with LEADS.open() as f:
        leads = list(csv.DictReader(f))

    li_existing = {}
    if LI.exists():
        with LI.open() as f:
            li_existing = {r["lead_id"]: r for r in csv.DictReader(f)}

    # Target leads: those without a LinkedIn URL (or all if ONLY_EMPTY=0)
    if ONLY_EMPTY:
        targets = [l for l in leads if not (li_existing.get(l["lead_id"], {}).get("linkedin_url") or "").strip()]
    else:
        targets = list(leads)
    print(f"total leads: {len(leads)}  |  targets (no LinkedIn yet): {len(targets)}")

    # Group by (school, state)
    by_school: dict[tuple[str, str], list[dict]] = {}
    for L in targets:
        k = ((L.get("school_name") or "").strip(), (L.get("state") or "").strip())
        if not k[0]:
            continue
        by_school.setdefault(k, []).append(L)

    print(f"unique schools: {len(by_school)}  (compression: {len(targets)/max(len(by_school),1):.1f}x)")
    print(f"workers: {WORKERS}")
    print()

    matched_total = 0
    lock = threading.Lock()

    def do_school(key: tuple[str, str]) -> tuple[tuple[str, str], list[dict]]:
        """Search one school, return list of new linkedin entries for matched leads."""
        school, state = key
        leads_here = by_school[key]
        try:
            hits, engine, q = search_school(school, state)
        except Exception as e:  # noqa: BLE001
            return key, [{"lead_id": L["lead_id"], "linkedin_url": "", "linkedin_confidence":"",
                         "source_query": f"err:{e.__class__.__name__}", "snippet":"", "engine":""}
                         for L in leads_here]

        # Match each lead to the best hit
        out = []
        for L in leads_here:
            first = (L.get("first_name") or "").lower()
            last = (L.get("last_name") or "").lower()
            best_url, best_snippet, best_conf = "", "", ""
            for url, snippet in hits:
                slug = slug_of(url)
                either, both = slug_matches(slug, first, last)
                if both:
                    best_url, best_snippet, best_conf = url, snippet, "high"
                    break
                if either and not best_url:
                    # store as fallback if no "both" comes along
                    best_url, best_snippet = url, snippet
                    # Snippet must mention school to elevate beyond needs_review
                    s = snippet.lower()
                    if (school.lower() in s) and any(k in s for k in COACH_KEYWORDS):
                        best_conf = "medium"
                    else:
                        best_conf = "needs_review"
            out.append({
                "lead_id": L["lead_id"],
                "linkedin_url": best_url,
                "linkedin_confidence": best_conf,
                "source_query": f"[school-batch] {q}",
                "snippet": best_snippet[:300],
                "engine": engine,
            })
        time.sleep(random.uniform(0.4, 0.8))
        return key, out

    new_entries: list[dict] = []
    completed_schools = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(do_school, k): k for k in by_school.keys()}
        for fut in as_completed(futures):
            key = futures[fut]
            try:
                _key, entries = fut.result()
            except Exception as e:  # noqa: BLE001
                entries = []
            with lock:
                completed_schools += 1
                matched_here = sum(1 for e in entries if e["linkedin_url"])
                matched_total += matched_here
                new_entries.extend(entries)
                school = key[0]
                print(f"[{completed_schools:4d}/{len(by_school)}] {school[:40]:40s}  matched {matched_here}/{len(entries)}", flush=True)

    # Merge into existing linkedin_results.csv
    for e in new_entries:
        li_existing[e["lead_id"]] = e

    tmp = LI.with_suffix(".csv.tmp")
    with tmp.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LI_COLS)
        w.writeheader()
        for r in li_existing.values():
            w.writerow({k: r.get(k, "") for k in LI_COLS})
    tmp.replace(LI)

    print()
    print(f"=== summary ===")
    print(f"  schools searched     : {completed_schools}")
    print(f"  total leads enriched : {matched_total}")
    hit_rate = matched_total / max(len(targets), 1) * 100
    print(f"  hit rate             : {hit_rate:.1f}%")


if __name__ == "__main__":
    main()
