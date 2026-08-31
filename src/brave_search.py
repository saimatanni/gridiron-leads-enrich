"""Brave Search API client — drop-in replacement for DDGS.

Use when Google/DDGS gets rate-limited. Brave's Search API has no
IP-level rate limiting and supports high concurrency (50 qps capacity).

Set BRAVE_API_KEY env var. Function `brave_search(query, count=10)` returns
a list of dicts with keys matching the DDGS shape:
    {title, body, href, url}

so existing first_linkedin_hit / collect_linkedin_hits logic works unchanged.
"""
from __future__ import annotations

import os
import time
from typing import Any

import httpx

BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
DEFAULT_TIMEOUT = httpx.Timeout(connect=4.0, read=10.0, write=4.0, pool=2.0)


def brave_search(
    query: str,
    count: int = 10,
    api_key: str | None = None,
    country: str = "US",
) -> list[dict[str, Any]]:
    """Run a single Brave web-search query. Returns DDGS-shaped result dicts."""
    key = api_key or os.environ.get("BRAVE_API_KEY", "")
    if not key:
        raise RuntimeError("BRAVE_API_KEY not set")
    if not query:
        return []
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": key,
    }
    params = {
        "q": query,
        "count": max(1, min(count, 20)),
        "country": country,
        "safesearch": "off",
    }
    try:
        with httpx.Client(timeout=DEFAULT_TIMEOUT) as client:
            r = client.get(BRAVE_ENDPOINT, headers=headers, params=params)
        if r.status_code == 429:
            # Tier-level rate limit. Brief pause then None — caller can retry.
            time.sleep(1.0)
            return []
        if r.status_code != 200:
            return []
        data = r.json()
    except Exception:  # noqa: BLE001
        return []
    web = (data.get("web") or {}).get("results") or []
    out = []
    for item in web:
        title = item.get("title", "") or ""
        url = item.get("url", "") or ""
        desc = item.get("description", "") or ""
        # Normalize to the shape DDGS produces; both `href` and `url` are populated
        out.append({
            "title": title,
            "body": desc,
            "href": url,
            "url": url,
        })
    return out


def is_available() -> bool:
    """Quick check that BRAVE_API_KEY is set."""
    return bool(os.environ.get("BRAVE_API_KEY"))
