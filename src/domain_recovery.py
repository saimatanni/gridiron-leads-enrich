"""For each lead whose email domain has no MX record (dead email domain),
find the school's REAL current website domain via Google, then re-test the
firstname.lastname email pattern against the new domain.

Approach for each dead-domain lead:
  1. Search Google: "<School Name>" "<State>" — find the school's actual site
  2. Extract first non-generic domain from results (skip wikipedia, niche.com, etc.)
  3. DNS MX check the candidate domain
  4. If MX exists, SMTP-probe firstname.lastname@candidate
  5. If valid/catch_all, save the recovered email

Outputs:
  data/domain_recovery.csv  -- lead_id, original_domain, new_domain, new_email,
                               new_status, smtp_message
"""
from __future__ import annotations

import csv
import os
import random
import smtplib
import socket
import string
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import dns.exception
import dns.resolver
from ddgs import DDGS

ROOT = Path(os.environ.get("GRIDIRON_DATASET_ROOT") or Path(__file__).resolve().parent.parent)
LEADS = ROOT / "data" / "leads_clean.csv"
EMAIL_VERIFY = ROOT / "data" / "email_verify_results.csv"
OUT = ROOT / "data" / "domain_recovery.csv"

OUT_FIELDS = ["lead_id", "original_email", "original_domain",
              "new_domain", "new_email", "new_status", "smtp_message", "source_url"]

GENERIC_DOMAINS = {
    # encyclopedias / aggregators / directories
    "wikipedia.org", "en.wikipedia.org", "niche.com", "greatschools.org",
    "schooldigger.com", "ratemyteachers.com", "publicschoolreview.com",
    "neighborhoodscout.com", "ratings.greatschools.org", "homefacts.com",
    "high-schools.com", "highschools.com", "schools.com", "u.s.news.com",
    "usnews.com", "petersons.com", "scholarship.com",
    # sports / sports media
    "maxpreps.com", "espn.com", "rivals.com", "247sports.com", "si.com",
    "cbssports.com", "foxsports.com", "nfhsnetwork.com", "varsity.com",
    "huskerextra.com", "athleticnet.com", "athletic.net", "milesplit.com",
    "athleticscholarships.net", "scout.com",
    # social / news / generic
    "facebook.com", "instagram.com", "twitter.com", "x.com", "youtube.com",
    "linkedin.com", "yelp.com", "tripadvisor.com", "google.com", "bing.com",
    "nytimes.com", "washingtonpost.com", "usatoday.com", "houstonchronicle.com",
    "har.com", "realtor.com", "zillow.com", "smore.com", "issuu.com",
    "amazon.com", "ebay.com", "tiktok.com", "reddit.com", "yahoo.com",
    "google.co.in", "bbb.org",
}

# Pattern hints that a domain looks school-related
SCHOOL_HINTS = ("isd", "schools", "highschool", "high-school", "k12", "academy",
                "preparatory", "prep")

# TLD/suffix patterns common for schools
SCHOOL_TLD_PATTERNS = (".edu", ".k12.", "isd.org", "isd.com", "isd.net",
                       "schools.org", "schools.com", "schools.net", ".sch.")

ENGINES = ("google", "bing", "yahoo")
FROM_ADDR = "verify-probe@example.com"
socket.setdefaulttimeout(10)
_mx_cache: dict[str, list[str]] = {}


def lookup_mx(domain: str) -> list[str]:
    if domain in _mx_cache:
        return _mx_cache[domain]
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=6)
        result = sorted([str(r.exchange).rstrip(".") for r in answers])
    except Exception:  # noqa: BLE001
        result = []
    _mx_cache[domain] = result
    return result


def random_local() -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=14))


def smtp_probe(email: str) -> tuple[str, str, str]:
    """Return (status, smtp_code, smtp_message)."""
    if "@" not in email:
        return "invalid", "", "malformed"
    domain = email.split("@", 1)[1]
    mx_hosts = lookup_mx(domain)
    if not mx_hosts:
        return "invalid", "", "no MX record"
    for mx in mx_hosts[:2]:
        try:
            with smtplib.SMTP(timeout=8) as s:
                s.connect(mx, 25)
                s.ehlo("example.com")
                code, msg = s.mail(FROM_ADDR)
                if code >= 400:
                    continue
                code, msg = s.rcpt(email)
                target_code, target_msg = code, msg.decode(errors="replace")[:120]
                # catch-all probe
                fake_code, _ = s.rcpt(f"{random_local()}@{domain}")
                if 200 <= target_code < 300:
                    if 200 <= fake_code < 300:
                        return "catch_all", str(target_code), target_msg
                    return "valid", str(target_code), target_msg
                if target_code >= 500:
                    return "invalid", str(target_code), target_msg
        except Exception as e:  # noqa: BLE001
            continue
    return "unknown", "", "all MX attempts failed"


def search(query: str) -> list[dict]:
    for engine in ENGINES:
        try:
            with DDGS() as ddg:
                results = list(ddg.text(query, backend=engine, max_results=10))
            if results:
                return results
        except Exception:  # noqa: BLE001
            time.sleep(0.6)
    return []


def candidate_domains(results: list[dict], school_name: str, city: str = "") -> list[tuple[str, str]]:
    """Return list of (domain, source_url) candidates ranked best-first.

    Strict rules:
      - Domain must match a school-TLD pattern (.edu, .k12., isd.*, schools.*) — OR —
      - Domain must contain a school-name token that is NOT just the city name.

    We never score based on the snippet/title (too easy to game: a news article
    on si.com about Argyle HS contains 'argyle' in the title but lives on si.com).
    """
    school_lc = school_name.lower()
    city_lc = (city or "").lower().strip()
    # Significant tokens: drop generic words and short tokens
    stop = {"high", "school", "academy", "the", "of", "and", "for", "at", "preparatory", "prep",
            "junior", "senior", "saint", "st", "k-12", "k12", "central", "north", "south", "east",
            "west", "county", "regional", "community", "district", "college"}
    tokens = []
    for t in school_lc.replace("-", " ").replace(".", " ").split():
        if len(t) >= 4 and t not in stop:
            tokens.append(t)
    # Distinctive tokens = school tokens that are NOT the city name. These are
    # the ones that should appear in the real domain (e.g. "argyle" for Argyle HS,
    # but NOT "chicago" if the school is in Chicago — that's just geographic noise).
    if city_lc:
        distinctive = [t for t in tokens if t not in city_lc and city_lc not in t]
    else:
        distinctive = tokens[:]
    candidates = []
    seen = set()
    for r in results:
        href = r.get("href") or r.get("url") or ""
        title = (r.get("title") or "").lower()
        if not href:
            continue
        try:
            netloc = urlparse(href).netloc.lower()
        except Exception:  # noqa: BLE001
            continue
        if netloc.startswith("www."):
            netloc = netloc[4:]
        if not netloc or netloc in seen:
            continue
        # blacklist generic
        if netloc in GENERIC_DOMAINS:
            continue
        if any(netloc.endswith("." + g) for g in GENERIC_DOMAINS):
            continue
        seen.add(netloc)

        score = 0
        tld_match = any(p in netloc for p in SCHOOL_TLD_PATTERNS)
        distinctive_in_domain = any(t in netloc for t in distinctive) if distinctive else False
        token_in_domain = any(t in netloc for t in tokens) if tokens else False

        # Title must mention the school (defends against universities/news/
        # other .edu and .k12.* domains that are NOT this specific school)
        title_has_school = bool(distinctive and any(t in title for t in distinctive))
        if not title_has_school and tokens:
            title_has_school = sum(1 for t in tokens if t in title) >= 2

        # Strict gate: a distinctive school token must appear in the domain.
        # Title-only matches are too easily fooled (e.g. butler.edu wrote
        # about Center Grove HS, so title contains "center grove" but
        # the domain isn't theirs).
        # Tradeoff: we miss some real cases (chisd.net for Cedar Hill HS,
        # where neither "cedar" nor "hill" appears in the abbreviation),
        # but we never recommend a wrong domain.
        if not distinctive_in_domain:
            continue

        if distinctive_in_domain:
            score += 10
        elif token_in_domain:
            score += 3
        if tld_match:
            score += 5
        if title_has_school:
            score += 3
        for hint in SCHOOL_HINTS:
            if hint in netloc:
                score += 2

        candidates.append((score, netloc, href))

    candidates.sort(key=lambda x: -x[0])
    return [(d, u) for _s, d, u in candidates]


def load_dead_leads() -> list[dict]:
    """Return lead dicts for emails that currently have no MX (dead domain)."""
    with EMAIL_VERIFY.open() as f:
        verify = {r["lead_id"]: r for r in csv.DictReader(f)}
    with LEADS.open() as f:
        leads = list(csv.DictReader(f))
    dead = []
    for lead in leads:
        v = verify.get(lead["lead_id"])
        if not v:
            continue
        if v["email_status"] == "invalid" and "no MX" in (v.get("smtp_message") or ""):
            dead.append(lead)
    return dead


def load_done() -> set[str]:
    if not OUT.exists():
        return set()
    with OUT.open() as f:
        return {r["lead_id"] for r in csv.DictReader(f)}


def main() -> None:
    dead = load_dead_leads()
    done = load_done()
    todo = [l for l in dead if l["lead_id"] not in done]
    print(f"dead-domain leads: {len(dead)} | already done: {len(done)} | todo: {len(todo)}")
    print()

    recovered = 0
    catch_all = 0
    is_new = not OUT.exists()
    with OUT.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=OUT_FIELDS)
        if is_new:
            w.writeheader()
        for i, lead in enumerate(todo, 1):
            school = lead.get("school_name", "")
            state = lead.get("state", "")
            first = (lead.get("first_name") or "").lower()
            last = (lead.get("last_name") or "").lower()
            orig_email = lead.get("email", "").lower()
            orig_domain = orig_email.split("@", 1)[-1] if "@" in orig_email else ""

            result = {
                "lead_id": lead["lead_id"],
                "original_email": orig_email,
                "original_domain": orig_domain,
                "new_domain": "",
                "new_email": "",
                "new_status": "",
                "smtp_message": "",
                "source_url": "",
            }

            # Step 1: find candidate domains
            q = f'"{school}" "{state}"' if state else f'"{school}"'
            results = search(q)
            cands = candidate_domains(results, school, city=lead.get("city", ""))

            tested = []
            found = False

            # Build expanded candidate list:
            #   for each found domain, also try the root domain (strip subdomain)
            expanded: list[tuple[str, str]] = []
            seen_dom = set()
            for d, u in cands[:6]:
                if d not in seen_dom:
                    expanded.append((d, u))
                    seen_dom.add(d)
                # If it's a subdomain, also try the registered root
                parts = d.split(".")
                if len(parts) >= 3:
                    # Heuristic root: last 2 labels for generic TLDs, last 3 for k12.xx.us
                    if parts[-2] == "k12" and parts[-1] in ("us", "ca"):
                        root = ".".join(parts[-4:])
                    elif parts[-1] in ("us", "ca", "uk") and len(parts) >= 4:
                        root = ".".join(parts[-3:])
                    else:
                        root = ".".join(parts[-2:])
                    if root != d and root not in seen_dom:
                        expanded.append((root, u))
                        seen_dom.add(root)

            # Email patterns to try, in order of common school usage
            email_patterns = [
                lambda f, l: f"{f}.{l}",
                lambda f, l: f"{f[0]}{l}" if f else l,
                lambda f, l: f"{l}",
                lambda f, l: f"{f}{l}",
            ]

            for new_domain, source_url in expanded:
                if new_domain == orig_domain:
                    continue
                if not lookup_mx(new_domain):
                    tested.append(f"{new_domain}=no_mx")
                    continue
                # Try each email pattern on this domain
                best_status_here = ""
                best_email_here = ""
                best_msg_here = ""
                for pat in email_patterns:
                    new_email = f"{pat(first, last)}@{new_domain}"
                    status, _code, msg = smtp_probe(new_email)
                    tested.append(f"{new_email}={status}")
                    if status == "valid":
                        best_status_here, best_email_here, best_msg_here = status, new_email, msg
                        break
                    if status == "catch_all" and not best_status_here:
                        best_status_here, best_email_here, best_msg_here = status, new_email, msg
                    if status == "unknown" and not best_status_here:
                        best_status_here, best_email_here, best_msg_here = status, new_email, msg
                if best_status_here in ("valid", "catch_all", "unknown"):
                    result.update({
                        "new_domain": new_domain,
                        "new_email": best_email_here,
                        "new_status": best_status_here,
                        "smtp_message": best_msg_here,
                        "source_url": source_url,
                    })
                    found = True
                    if best_status_here == "valid": recovered += 1
                    elif best_status_here == "catch_all": catch_all += 1
                    break

            if not found:
                result["smtp_message"] = "; ".join(tested[:8]) if tested else "no candidates"

            w.writerow(result)
            f.flush()
            status_tag = result["new_status"] or "miss"
            new_email_short = result["new_email"][:50] if result["new_email"] else "-"
            print(f"[{i:3d}/{len(todo)}] {first:>10s}.{last:<12s} @ {school[:30]:30s} -> {status_tag:10s} {new_email_short}", flush=True)
            time.sleep(random.uniform(0.8, 1.3))

    print()
    print(f"=== summary ===")
    print(f"  recovered (valid):   {recovered}")
    print(f"  recovered (catch_all): {catch_all}")
    print(f"  no recovery:         {len(todo) - recovered - catch_all}")


if __name__ == "__main__":
    main()
