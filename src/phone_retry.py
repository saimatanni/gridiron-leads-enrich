"""Second-pass phone discovery for leads we missed.

Three new strategies:
  A. Google search   →  "School Name" "athletic department phone"  → extract phone from snippet
  B. Extra URL paths →  /our-coaches, /football, /high-school-athletics, /sports, /high-school-coaches
  C. MaxPreps re-scrape → for leads with a _maxpreps_url in source.xlsx,
                          fetch the team page and look for staff phone

Targets: leads currently without phone_school AND phone_direct.

Reads:   data/leads_clean.csv  +  data/phone_results.csv  +  source.xlsx (for maxpreps URLs)
Writes:  data/phone_results.csv (updates in place, preserves rows that already have a phone)
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
import openpyxl
from ddgs import DDGS
from selectolax.parser import HTMLParser

WORKERS = int(os.environ.get("GRIDIRON_PHONE_WORKERS", "5"))
WRITE_EVERY = 10  # flush CSV after this many lead updates

ROOT = Path(os.environ.get("GRIDIRON_DATASET_ROOT") or Path(__file__).resolve().parent.parent)
LEADS = ROOT / "data" / "leads_clean.csv"
PHONES = ROOT / "data" / "phone_results.csv"
SRC = ROOT / "source.xlsx"

OUT_FIELDS = ["lead_id", "phone_school", "phone_direct", "source_page", "notes"]

EXTRA_PATHS = [
    "/our-coaches", "/football", "/high-school-athletics", "/sports",
    "/high-school-coaches", "/athletics/football", "/team", "/staff/athletics",
    "/about/contact-us", "/about-us/contact",
]

PHONE_RE = re.compile(r"(?:\+?1[\s.-]?)?\(?(\d{3})\)?[\s.-]?(\d{3})[\s.-]?(\d{4})")
ENGINES = ("google", "bing", "yahoo")

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

PHONE_BLACKLIST = {"8005551234", "5555555555", "1234567890", "0000000000"}

# Valid US/Canada NANP area codes (NPA). Source: nationalnanpa.com / Wikipedia, 2025.
# An area code is valid if it appears here; junk like 300, 638 won't.
VALID_AREA_CODES = {
    "201","202","203","204","205","206","207","208","209","210","212","213","214","215","216","217","218","219",
    "220","223","224","225","226","227","228","229","231","234","236","239","240","242","246","248","249","250",
    "251","252","253","254","256","260","262","263","264","267","268","269","270","272","274","276","279","281",
    "283","284","289","290","291","292","293","294","295","296","297","298","299",
    "301","302","303","304","305","306","307","308","309","310","312","313","314","315","316","317","318","319",
    "320","321","323","325","326","327","329","330","331","332","334","336","337","339","340","341","343","345",
    "346","347","350","351","352","353","354","360","361","363","364","365","367","368","369",
    "380","382","385","386","387","389",
    "401","402","403","404","405","406","407","408","409","410","412","413","414","415","416","417","418","419",
    "423","424","425","428","430","431","432","434","435","436","437","438","440","441","442","443","445","447",
    "448","450","458","463","464","468","469","470","472","473","474","475","478","479","480","481","482","483",
    "484",
    "501","502","503","504","505","506","507","508","509","510","512","513","514","515","516","517","518","519",
    "520","530","531","534","539","540","541","548","551","557","559","561","562","563","564","567","570","571",
    "572","573","574","575","579","580","581","582","584","585","586","587","588","589",
    "601","602","603","604","605","606","607","608","609","610","612","613","614","615","616","617","618","619",
    "620","623","626","628","629","630","631","636","639","640","641","645","646","647","649","650","651","656",
    "657","658","659","660","661","662","664","667","669","670","671","672","678","680","681","682","683","684",
    "687",
    "701","702","703","704","705","706","707","708","709","712","713","714","715","716","717","718","719","720",
    "721","724","725","726","727","728","730","731","732","734","737","740","742","743","747","753","754","757",
    "758","760","762","763","765","767","769","770","771","772","773","774","775","778","779","780","781","782",
    "784","785","786","787","800","801","802","803","804","805","806","807","808","809","810","812","813","814",
    "815","816","817","818","819","820","822","825","826","828","829","830","831","832","833","835","836","838",
    "839","840","843","844","845","847","848","849","850","854","855","856","857","858","859","860","862","863",
    "864","865","866","867","868","869","870","872","873","876","877","878","888","901","902","903","904","906",
    "907","908","909","910","912","913","914","915","916","917","918","919","920","925","928","929","930","931",
    "934","936","937","938","939","940","941","943","945","947","948","949","951","952","954","956","959","970",
    "971","972","973","978","979","980","983","984","985","986","989",
}


def is_valid(area: str) -> bool:
    """Strict NANP validation — area code must be in the assigned list."""
    return area in VALID_AREA_CODES


def find_phone(text: str, near_name: str = "") -> tuple[str, str]:
    """Return (school_phone, direct_phone). Direct = phone within 200 chars of name."""
    school, direct = "", ""
    name_lc = (near_name or "").lower().strip()
    for m in PHONE_RE.finditer(text):
        area, mid, last = m.group(1), m.group(2), m.group(3)
        raw = area + mid + last
        if raw in PHONE_BLACKLIST or not is_valid(area):
            continue
        phone = f"({area}) {mid}-{last}"
        if name_lc and not direct:
            ctx = text[max(0, m.start() - 200): m.end() + 200].lower()
            if name_lc in ctx:
                direct = phone
                continue
        if not school:
            school = phone
        if school and direct:
            break
    return school, direct


def fetch(client: httpx.Client, url: str) -> str | None:
    try:
        r = client.get(url, timeout=httpx.Timeout(connect=4.0, read=6.0, write=4.0, pool=2.0),
                       follow_redirects=True)
        if r.status_code == 200 and "text/html" in r.headers.get("content-type", ""):
            return r.text[:1_000_000]
    except Exception:  # noqa: BLE001
        return None
    return None


def search(query: str) -> tuple[list[dict], str]:
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


def google_phone_for_school(lead: dict) -> tuple[str, str]:
    """Returns (phone, source) from a Google search of the school name.

    Strict: the phone must appear in a result whose snippet also contains
    the school name (or domain). Otherwise we're just grabbing random
    digit sequences from unrelated results.
    """
    school = (lead.get("school_name") or "").strip()
    school_lc = school.lower()
    domain = (lead.get("school_domain") or "").lower()
    state = lead.get("state", "")
    if not school:
        return "", ""

    queries = [
        f'"{school}" athletic department phone',
        f'"{school}" football coach contact phone',
        f'"{school}" "{state}" phone',
    ] if state else [
        f'"{school}" athletic department phone',
    ]

    for q in queries:
        results, _engine = search(q)
        full_name = f"{lead['first_name']} {lead['last_name']}"
        for r in results:
            blob = (r.get("title", "") + " " + (r.get("body") or "")).strip()
            blob_lc = blob.lower()
            # Snippet must mention the school OR the domain — proves the
            # phone in this snippet is for this school, not a random hit
            if school_lc not in blob_lc and (not domain or domain not in blob_lc):
                continue
            sp, dp = find_phone(blob, near_name=full_name)
            if dp:
                return dp, f"google:{q}"
            if sp:
                return sp, f"google:{q}"
        time.sleep(0.5)
    return "", ""


def extra_paths_for_domain(lead: dict, client: httpx.Client) -> tuple[str, str, str]:
    """Try a handful of less-common URL paths on the school's domain."""
    domain = lead.get("school_domain", "").strip()
    if not domain:
        return "", "", ""
    base = f"https://{domain}"
    full_name = f"{lead['first_name']} {lead['last_name']}"
    for path in EXTRA_PATHS:
        url = urljoin(base, path)
        html = fetch(client, url)
        if not html:
            continue
        try:
            text = HTMLParser(html).text(separator=" ", strip=True)
        except Exception:  # noqa: BLE001
            continue
        sp, dp = find_phone(text, near_name=full_name)
        if dp or sp:
            return sp, dp, url
    return "", "", ""


def maxpreps_phone(maxpreps_url: str, lead: dict, client: httpx.Client) -> tuple[str, str, str]:
    if not maxpreps_url:
        return "", "", ""
    html = fetch(client, maxpreps_url)
    if not html:
        return "", "", ""
    try:
        text = HTMLParser(html).text(separator=" ", strip=True)
    except Exception:  # noqa: BLE001
        return "", "", ""
    full_name = f"{lead['first_name']} {lead['last_name']}"
    sp, dp = find_phone(text, near_name=full_name)
    return sp, dp, maxpreps_url


def load_maxpreps_urls() -> dict[str, str]:
    """Lead_id -> _maxpreps_url from source.xlsx. Returns {} if columns absent."""
    try:
        wb = openpyxl.load_workbook(SRC, read_only=True, data_only=True)
    except Exception:  # noqa: BLE001
        return {}
    ws = wb[wb.sheetnames[0]]
    rows = ws.iter_rows(values_only=True)
    try:
        header = list(next(rows))
    except StopIteration:
        return {}
    lc = {(h or "").strip().lower(): i for i, h in enumerate(header) if h is not None}
    id_i = lc.get("id") or lc.get("lead_id")
    mp_i = lc.get("_maxpreps_url") or lc.get("maxpreps_url")
    if id_i is None or mp_i is None:
        return {}  # not a MaxPreps-sourced file, skip this pass
    out: dict[str, str] = {}
    for row in rows:
        if not row:
            continue
        v = row[mp_i] if mp_i < len(row) else None
        lid = row[id_i] if id_i < len(row) else None
        if v and lid:
            out[str(lid).strip()] = str(v).strip()
    return out


def scrub_invalid(current: dict[str, dict]) -> int:
    """Blank out phone fields with invalid NANP area codes. Returns count scrubbed."""
    scrubbed = 0
    for lid, r in current.items():
        for field in ("phone_school", "phone_direct"):
            v = (r.get(field) or "").strip()
            if not v:
                continue
            m = PHONE_RE.match(v)
            if not m or not is_valid(m.group(1)):
                r[field] = ""
                scrubbed += 1
    return scrubbed


def _school_key(lead: dict) -> str:
    """Group key for the school-cache. Same school+state → one lookup."""
    return f"{(lead.get('school_name') or '').strip().lower()}|{(lead.get('state') or '').strip().lower()}"


def write_csv(current: dict[str, dict]) -> None:
    """Atomic-ish write of the phone_results.csv file."""
    tmp = PHONES.with_suffix(".csv.tmp")
    with tmp.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=OUT_FIELDS)
        w.writeheader()
        for r in current.values():
            w.writerow({k: r.get(k, "") for k in OUT_FIELDS})
    tmp.replace(PHONES)


def lookup_school_phone(school_key: str, sample_lead: dict) -> tuple[str, str]:
    """Single Google search for the school's main phone. Cached per school."""
    return google_phone_for_school(sample_lead)


def main() -> None:
    with LEADS.open() as f:
        leads_by_id = {r["lead_id"]: r for r in csv.DictReader(f)}
    with PHONES.open() as f:
        current = {r["lead_id"]: r for r in csv.DictReader(f)}

    scrubbed = scrub_invalid(current)
    if scrubbed:
        print(f"scrubbed {scrubbed} invalid phones (area code not in NANP list)")

    targets = [lid for lid, r in current.items()
               if not (r.get("phone_school") or r.get("phone_direct"))]
    print(f"current phone rows: {len(current)}")
    print(f"  with phone     : {sum(1 for r in current.values() if r.get('phone_school') or r.get('phone_direct'))}")
    print(f"  without phone  : {len(targets)}")

    # ---- School cache: group targets by (school_name, state) ----
    school_to_lids: dict[str, list[str]] = {}
    for lid in targets:
        lead = leads_by_id.get(lid, {})
        if not lead.get("school_name"):
            continue
        school_to_lids.setdefault(_school_key(lead), []).append(lid)

    unique_schools = list(school_to_lids.keys())
    print(f"  unique schools needing lookup: {len(unique_schools)} (saves {len(targets) - len(unique_schools)} duplicate searches)")

    maxpreps = load_maxpreps_urls()
    print(f"  with maxpreps url available: {sum(1 for lid in targets if maxpreps.get(lid))}")
    print(f"  parallel workers: {WORKERS}")
    print()

    new_phones = 0
    by_source = {"google_cached": 0, "google": 0, "extra_paths": 0, "maxpreps": 0}
    lock = threading.Lock()
    updates_since_flush = 0

    def process_school(school_key: str) -> tuple[str, str, str]:
        """Returns (school_key, phone, source). Phone may be ''."""
        sample_lid = school_to_lids[school_key][0]
        sample_lead = leads_by_id[sample_lid]
        phone, src = lookup_school_phone(school_key, sample_lead)
        time.sleep(random.uniform(0.3, 0.7))  # gentle on Google
        return school_key, phone, src

    # ---- Pass A (parallel) — one Google search per unique school ----
    print(f"[Pass A] parallel Google search across {len(unique_schools)} schools…")
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(process_school, sk): sk for sk in unique_schools}
        done = 0
        for fut in as_completed(futures):
            sk = futures[fut]
            try:
                _sk, phone, src = fut.result()
            except Exception as e:  # noqa: BLE001
                phone, src = "", ""
                print(f"  err {sk}: {e.__class__.__name__}", file=sys.stderr)
            done += 1
            lids = school_to_lids[sk]
            sample_lead = leads_by_id[lids[0]]
            tag = "FOUND" if phone else "miss "
            print(f"  [{done:3d}/{len(unique_schools)}] {sample_lead.get('school_name','')[:35]:35s} → {tag} {phone or '-'} (applies to {len(lids)} leads)", flush=True)
            if phone:
                with lock:
                    for lid in lids:
                        cur = current[lid]
                        cur["phone_school"] = phone
                        cur["source_page"] = src
                        cur["notes"] = "found via google retry (school-cached)"
                        new_phones += 1
                        by_source["google_cached"] += 1
                        updates_since_flush += 1
                    if updates_since_flush >= WRITE_EVERY:
                        write_csv(current)
                        updates_since_flush = 0

    # Flush after Pass A
    write_csv(current)
    updates_since_flush = 0

    # ---- Pass B — remaining leads without phone, try extra URL paths + maxpreps ----
    remaining = [lid for lid in targets
                 if not (current[lid].get("phone_school") or current[lid].get("phone_direct"))]
    print()
    print(f"[Pass B+C] sequential fallback for {len(remaining)} stragglers…")

    with httpx.Client(headers=HEADERS, verify=True) as client:
        for i, lid in enumerate(remaining, 1):
            lead = leads_by_id.get(lid, {})
            if not lead:
                continue
            cur = current[lid]
            found = False

            # Pass B — extra URL paths
            if lead.get("school_domain"):
                sp, dp, src = extra_paths_for_domain(lead, client)
                if dp or sp:
                    if dp:
                        cur["phone_direct"] = dp
                    if sp and not cur.get("phone_school"):
                        cur["phone_school"] = sp
                    cur["source_page"] = src
                    cur["notes"] = "found via extra paths"
                    new_phones += 1
                    by_source["extra_paths"] += 1
                    found = True

            # Pass C — MaxPreps re-scrape
            if not found and maxpreps.get(lid):
                sp, dp, src = maxpreps_phone(maxpreps[lid], lead, client)
                if dp or sp:
                    if dp:
                        cur["phone_direct"] = dp
                    if sp and not cur.get("phone_school"):
                        cur["phone_school"] = sp
                    cur["source_page"] = src
                    cur["notes"] = "found via maxpreps retry"
                    new_phones += 1
                    by_source["maxpreps"] += 1
                    found = True

            status = "FOUND" if found else "miss"
            ph = cur.get("phone_direct") or cur.get("phone_school") or "-"
            print(f"  [{i:3d}/{len(remaining)}] {lead.get('first_name',''):>10s} {lead.get('last_name',''):<12s} @ {lead.get('school_domain','')[:30]:30s}  {status:6s} {ph}", flush=True)
            updates_since_flush += 1
            if updates_since_flush >= WRITE_EVERY:
                write_csv(current)
                updates_since_flush = 0
            time.sleep(random.uniform(0.3, 0.6))

    write_csv(current)
    print()
    print("=== summary ===")
    print(f"  new phones found: {new_phones}")
    print(f"  by source: {by_source}")


if __name__ == "__main__":
    main()
