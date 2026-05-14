"""SMTP verify each lead's email.

Two passes:
1. MX lookup — if no MX, mark invalid.
2. SMTP RCPT TO probe — connect to MX, run EHLO + MAIL FROM + RCPT TO.
   Read response code:
     250 / 2xx       -> valid
     550 / 5xx user  -> invalid
     4xx temp        -> unknown (greylist)
     catch-all hint  -> catch_all

We cache MX results per domain to avoid re-resolving.
Resumable: skips lead_ids already present.

Outputs data/email_verify_results.csv:
    lead_id, email, email_status, smtp_code, smtp_message
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
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import dns.resolver
import dns.exception

ROOT = Path(os.environ.get("GRIDIRON_DATASET_ROOT") or Path(__file__).resolve().parent.parent)
LEADS = ROOT / "data" / "leads_clean.csv"
OUT = ROOT / "data" / "email_verify_results.csv"

OUT_FIELDS = ["lead_id", "email", "email_status", "smtp_code", "smtp_message"]

FROM_ADDR = "verify-probe@example.com"

socket.setdefaulttimeout(10)
_mx_cache: dict[str, list[str]] = {}


def lookup_mx(domain: str) -> list[str]:
    if domain in _mx_cache:
        return _mx_cache[domain]
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=8)
        hosts = sorted(
            ((r.preference, str(r.exchange).rstrip(".")) for r in answers),
            key=lambda x: x[0],
        )
        result = [h for _, h in hosts]
    except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN,
            dns.resolver.NoNameservers, dns.exception.Timeout):
        result = []
    except Exception:  # noqa: BLE001
        result = []
    _mx_cache[domain] = result
    return result


def random_local() -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=14))


def probe_email(email: str) -> dict:
    """Return dict with status, code, message."""
    if "@" not in email:
        return {"email_status": "invalid", "smtp_code": "", "smtp_message": "malformed"}
    domain = email.split("@", 1)[1].lower()
    mx_hosts = lookup_mx(domain)
    if not mx_hosts:
        return {"email_status": "invalid", "smtp_code": "", "smtp_message": "no MX record"}

    last_code, last_msg = "", ""
    for mx in mx_hosts[:2]:
        try:
            with smtplib.SMTP(timeout=10) as s:
                s.connect(mx, 25)
                s.ehlo("example.com")
                code, msg = s.mail(FROM_ADDR)
                if code >= 400:
                    last_code, last_msg = str(code), msg.decode(errors="replace")[:120]
                    continue
                code, msg = s.rcpt(email)
                target_code, target_msg = code, msg.decode(errors="replace")[:120]
                # Now probe a random local-part to detect catch-all
                fake = f"{random_local()}@{domain}"
                fake_code, fake_msg = s.rcpt(fake)
                fake_msg_s = fake_msg.decode(errors="replace")[:120]

                if 200 <= target_code < 300:
                    if 200 <= fake_code < 300:
                        return {"email_status": "catch_all", "smtp_code": str(target_code), "smtp_message": target_msg}
                    return {"email_status": "valid", "smtp_code": str(target_code), "smtp_message": target_msg}
                if target_code >= 500:
                    return {"email_status": "invalid", "smtp_code": str(target_code), "smtp_message": target_msg}
                # 4xx — temporary
                last_code, last_msg = str(target_code), target_msg
        except (smtplib.SMTPException, socket.timeout, socket.gaierror, ConnectionError, OSError) as e:
            last_code, last_msg = "", f"{e.__class__.__name__}: {str(e)[:100]}"
            continue

    return {"email_status": "unknown", "smtp_code": last_code, "smtp_message": last_msg}


def load_done(path: Path) -> set[str]:
    if not path.exists():
        return set()
    with path.open() as f:
        return {r["lead_id"] for r in csv.DictReader(f)}


def verify_one(lead: dict) -> dict:
    res = probe_email(lead["email"])
    return {
        "lead_id": lead["lead_id"],
        "email": lead["email"],
        **res,
    }


def main(workers: int = 8) -> None:
    with LEADS.open() as f:
        leads = list(csv.DictReader(f))
    done = load_done(OUT)
    todo = [l for l in leads if l["lead_id"] not in done]
    print(f"leads total: {len(leads)} | done: {len(done)} | todo: {len(todo)}")

    is_new = not OUT.exists()
    with OUT.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=OUT_FIELDS)
        if is_new:
            w.writeheader()
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(verify_one, lead): lead for lead in todo}
            for i, fut in enumerate(as_completed(futures), 1):
                lead = futures[fut]
                try:
                    result = fut.result()
                except Exception as e:  # noqa: BLE001
                    result = {"lead_id": lead["lead_id"], "email": lead["email"],
                              "email_status": "error", "smtp_code": "",
                              "smtp_message": f"{e.__class__.__name__}: {str(e)[:100]}"}
                w.writerow(result)
                f.flush()
                print(f"[{i:3d}/{len(todo)}] {result['email']:50s} -> {result['email_status']:10s} {result['smtp_code']} {result['smtp_message'][:40]}", flush=True)


if __name__ == "__main__":
    main()
