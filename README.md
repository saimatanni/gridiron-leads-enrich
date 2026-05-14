# Gridiron leads — enrichment pipeline

Free-only enrichment of the real (non-synthetic) leads from
`gridiron-leads-2026-05-08.xlsx`. Produces a marketing-ready Excel file,
an HTML report, and a Streamlit dashboard.

## Pipeline

```
source.xlsx                                          ──┐
        │                                              │
        ▼                                              │
src/extract.py     ─►  data/leads_clean.csv            │  drops 20,154 synthetic rows;
                                                       │  dedupes by (school, name)
        │                                              │
        ├────────────────┬───────────────┐             │
        ▼                ▼               ▼             │
linkedin_find.py     phone_find.py   email_verify.py   │  three independent passes
        │                │               │             │
        ▼                ▼               ▼             │
linkedin_results   phone_results   email_verify_results │
        \\                |                /            │
         \\               |               /             │
          ▼               ▼              ▼             │
                src/merge.py                           │  joins into enriched.csv
                       │                               │
        ┌──────────────┼──────────────┐                │
        ▼              ▼              ▼                │
   build_xlsx.py   build_report.py   app.py            │
        │              │              │                │
        ▼              ▼              ▼                │
  Excel file     HTML report     Streamlit UI        ──┘
```

## How to run

```bash
# 1. Initial extract (sync — fast)
uv run python src/extract.py

# 2. Enrichment (slow — run in background, resumable)
nohup uv run python src/linkedin_find.py > logs/linkedin.log 2>&1 &
nohup uv run python src/phone_find.py    > logs/phone.log    2>&1 &
nohup uv run python src/email_verify.py  > logs/email.log    2>&1 &

# 3. Finalize once all three complete
uv run python src/finalize.py

# 4. Launch interactive UI
./run_app.sh                                 # serves on 0.0.0.0:8501
# From your laptop: ssh -L 8501:localhost:8501 saimadevserver
# Then open http://localhost:8501
```

## Outputs

- `output/gridiron-leads-enriched.xlsx` — main deliverable, 6 tabs
- `output/enrichment-report.html` — self-contained visual summary
- Streamlit dashboard on port 8501 — interactive filtering & downloads

## Outreach tiers

| Tier | Criteria | Action |
|---|---|---|
| A | LinkedIn (high/medium) + valid email | LinkedIn DM, email follow-up |
| B | Phone + valid email | Call school AD office, email follow-up |
| C | Valid email only | Email-only sequence |
| D | LinkedIn or phone, bad email | Single-channel, lower priority |
| E | Nothing usable | Manual research or deprioritize |

## Notes

- All enrichment scripts are **resumable** — re-running picks up where it left off.
- Zero paid APIs. Search via DDGS multi-engine rotation (Google/Bing/Yahoo).
- Email verification uses MX lookup + SMTP RCPT TO probe.
- LinkedIn matches require slug-to-name verification to reduce wrong-person hits.
