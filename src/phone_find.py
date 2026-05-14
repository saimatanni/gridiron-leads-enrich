"""Find phone numbers by scraping each lead's school website.

For each lead with company_domain, try the common athletics/staff paths.
Extract phone patterns. If a phone appears near the lead's name, mark it
as direct; otherwise it's the school/athletic-dept main line.

Outputs data/phone_results.csv (resumable):
    lead_id, phone_school, phone_direct, source_page, notes
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
from urllib.parse import urljoin

import httpx
from selectolax.parser import HTMLParser

ROOT = Path(os.environ.get("GRIDIRON_DATASET_ROOT") or Path(__file__).resolve().parent.parent)
LEADS = ROOT / "data" / "leads_clean.csv"
OUT = ROOT / "data" / "phone_results.csv"

OUT_FIELDS = ["lead_id", "phone_school", "phone_direct", "source_page", "notes"]
WORKERS = int(os.environ.get("GRIDIRON_PHONE_FIND_WORKERS", "8"))
WRITE_EVERY = 10

# Paths to try on each school domain, in priority order.
PATHS = [
    "/athletics", "/athletics/", "/athletics/staff",
    "/athletics/coaches", "/athletics/staff-directory",
    "/staff", "/staff-directory", "/our-staff",
    "/coaches", "/coaching-staff",
    "/contact", "/contact-us", "/about/contact",
    "/",  # homepage as last resort
]

PHONE_RE = re.compile(
    r"(?:\+?1[\s.-]?)?\(?(\d{3})\)?[\s.-]?(\d{3})[\s.-]?(\d{4})"
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Quick-reject anything in this set — toll-free or generic placeholders
PHONE_BLACKLIST = {
    "8005551234", "5555555555", "1234567890", "0000000000",
}


def fmt_phone(area: str, mid: str, last: str) -> str:
    return f"({area}) {mid}-{last}"


def is_valid(area: str) -> bool:
    """Reject phone numbers with invalid US area codes (start with 0 or 1)."""
    return area[0] not in "01"


def fetch(client: httpx.Client, url: str) -> str | None:
    try:
        r = client.get(
            url,
            timeout=httpx.Timeout(connect=4.0, read=6.0, write=4.0, pool=2.0),
            follow_redirects=True,
        )
        if r.status_code == 200 and "text/html" in r.headers.get("content-type", ""):
            # Cap payload at 1 MB to avoid memory explosions on big district sites
            return r.text[:1_000_000]
    except Exception:  # noqa: BLE001
        return None
    return None


def find_phones_in_html(html: str, lead: dict) -> tuple[str, str]:
    """Return (school_phone, direct_phone). Direct = phone within ~200 chars of the name."""
    tree = HTMLParser(html)
    # Try the visible text only
    text = tree.text(separator=" ", strip=True) if tree.body else html
    text_lc = text.lower()

    school_phone = ""
    direct_phone = ""

    full_name = f"{lead['first_name']} {lead['last_name']}".lower().strip()
    last = lead["last_name"].lower()

    for m in PHONE_RE.finditer(text):
        area, mid, last_4 = m.group(1), m.group(2), m.group(3)
        raw = area + mid + last_4
        if raw in PHONE_BLACKLIST or not is_valid(area):
            continue
        phone = fmt_phone(area, mid, last_4)
        start = m.start()
        # Look 200 chars before and after for the lead's name
        ctx_start = max(0, start - 200)
        ctx_end = min(len(text), start + 200)
        ctx = text_lc[ctx_start:ctx_end]
        if (full_name and full_name in ctx) or (last and len(last) >= 4 and last in ctx):
            if not direct_phone:
                direct_phone = phone
                continue
        if not school_phone:
            school_phone = phone
        if school_phone and direct_phone:
            break

    return school_phone, direct_phone


def load_done(path: Path) -> set[str]:
    if not path.exists():
        return set()
    with path.open() as f:
        return {r["lead_id"] for r in csv.DictReader(f)}


def process(lead: dict, client: httpx.Client, per_lead_budget_s: float = 25.0) -> dict:
    domain = lead.get("school_domain", "").strip()
    if not domain:
        return {"lead_id": lead["lead_id"], "phone_school": "", "phone_direct": "",
                "source_page": "", "notes": "no domain"}

    base = f"https://{domain}"
    visited = set()
    school_phone, direct_phone, source_page, notes = "", "", "", ""
    start = time.monotonic()

    for path in PATHS:
        if time.monotonic() - start > per_lead_budget_s:
            notes = "scrape time budget exceeded"
            break
        url = urljoin(base, path)
        if url in visited:
            continue
        visited.add(url)
        html = fetch(client, url)
        if not html:
            continue
        sp, dp = find_phones_in_html(html, lead)
        if dp and not direct_phone:
            direct_phone, source_page = dp, url
        if sp and not school_phone:
            school_phone, source_page = sp, source_page or url
        if direct_phone:
            break  # direct match is the win condition

    if not (school_phone or direct_phone) and not notes:
        notes = "no phone found on school site"

    return {
        "lead_id": lead["lead_id"],
        "phone_school": school_phone,
        "phone_direct": direct_phone,
        "source_page": source_page,
        "notes": notes,
    }


def main() -> None:
    with LEADS.open() as f:
        leads = list(csv.DictReader(f))
    done = load_done(OUT)
    todo = [l for l in leads if l["lead_id"] not in done]
    print(f"leads total: {len(leads)} | done: {len(done)} | todo: {len(todo)} | workers: {WORKERS}")

    # Group leads by school_domain so we hit each domain once and share results
    domain_to_leads: dict[str, list[dict]] = {}
    no_domain: list[dict] = []
    for L in todo:
        d = (L.get("school_domain") or "").strip().lower()
        if d:
            domain_to_leads.setdefault(d, []).append(L)
        else:
            no_domain.append(L)
    print(f"  unique domains: {len(domain_to_leads)} (saves {len(todo)-len(domain_to_leads)-len(no_domain)} repeat fetches)")
    print(f"  leads without domain: {len(no_domain)}")

    is_new = not OUT.exists()
    results_buffer: list[dict] = []
    lock = threading.Lock()
    completed_domains = 0

    def write_buffer(f, w, flush=False):
        """Flush results_buffer to disk."""
        if not results_buffer:
            return
        for row in results_buffer:
            w.writerow(row)
        f.flush()
        results_buffer.clear()

    def process_domain(domain: str, leads_at_domain: list[dict]) -> list[dict]:
        """Fetch the school site ONCE; then score phones per-lead (direct vs school)."""
        out: list[dict] = []
        # Use the first lead just for path-traversal context; phones get attributed per-lead
        with httpx.Client(headers=HEADERS, http2=False, verify=True) as client:
            base = f"https://{domain}"
            visited = set()
            html_by_url: dict[str, str] = {}
            start = time.monotonic()
            for path in PATHS:
                if time.monotonic() - start > 25.0:
                    break
                url = urljoin(base, path)
                if url in visited:
                    continue
                visited.add(url)
                html = fetch(client, url)
                if html:
                    html_by_url[url] = html
                    # Stop early once we have a couple of pages with content
                    if len(html_by_url) >= 3:
                        break
        # For each lead at this domain, find their direct phone in any fetched page
        for L in leads_at_domain:
            school_phone, direct_phone, source_page, notes = "", "", "", ""
            for url, html in html_by_url.items():
                sp, dp = find_phones_in_html(html, L)
                if dp and not direct_phone:
                    direct_phone, source_page = dp, url
                if sp and not school_phone:
                    school_phone = sp
                    if not source_page:
                        source_page = url
                if direct_phone:
                    break
            if not (school_phone or direct_phone):
                notes = "no phone found on school site" if html_by_url else "school site unreachable"
            out.append({
                "lead_id": L["lead_id"],
                "phone_school": school_phone,
                "phone_direct": direct_phone,
                "source_page": source_page,
                "notes": notes,
            })
        return out

    with OUT.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=OUT_FIELDS)
        if is_new:
            w.writeheader()

        # ---- Parallel domain fetches ----
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            futures = {ex.submit(process_domain, d, ls): d for d, ls in domain_to_leads.items()}
            for fut in as_completed(futures):
                d = futures[fut]
                try:
                    rows = fut.result()
                except Exception as e:  # noqa: BLE001
                    rows = [{"lead_id": L["lead_id"], "phone_school":"", "phone_direct":"",
                             "source_page":"", "notes": f"err:{e.__class__.__name__}"} for L in domain_to_leads[d]]
                with lock:
                    completed_domains += 1
                    for r in rows:
                        results_buffer.append(r)
                        L = next((x for x in domain_to_leads[d] if x["lead_id"] == r["lead_id"]), {})
                        tag = []
                        if r["phone_direct"]: tag.append(f"D:{r['phone_direct']}")
                        if r["phone_school"]: tag.append(f"S:{r['phone_school']}")
                        if not tag: tag.append(r["notes"] or "-")
                        print(f"[{completed_domains:3d}/{len(domain_to_leads)}] {L.get('first_name',''):12s} {L.get('last_name',''):14s} @ {d[:30]:30s}  {' '.join(tag)}", flush=True)
                    if len(results_buffer) >= WRITE_EVERY:
                        write_buffer(f, w)

        # ---- Write any remaining buffered rows + the no-domain rows ----
        with lock:
            write_buffer(f, w)
        for L in no_domain:
            w.writerow({
                "lead_id": L["lead_id"], "phone_school":"", "phone_direct":"",
                "source_page":"", "notes":"no domain",
            })
        f.flush()


if __name__ == "__main__":
    main()
