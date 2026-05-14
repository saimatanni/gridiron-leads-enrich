"""Streamlit dashboard for the gridiron leads pipeline.

Multi-dataset: select between the original "gridiron-2026-05-08" data and any
uploaded datasets. Upload tab accepts xlsx/csv, kicks off the pipeline in the
background, and a live-progress panel shows each phase finishing in real time.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATASETS_DIR = PROJECT_ROOT / "datasets"
DATASETS_DIR.mkdir(exist_ok=True)


# --- Dataset resolution ---------------------------------------------------- #

@dataclass
class Dataset:
    name: str            # "" for original
    label: str           # display name in the selector
    root: Path
    source: Path
    enriched: Path
    synthetic: Path
    email_verify: Path
    leads_clean: Path
    xlsx: Path
    html: Path
    verification: Path
    verification_pdf: Path
    status: Path

    @classmethod
    def from_root(cls, name: str, label: str, root: Path) -> "Dataset":
        return cls(
            name=name,
            label=label,
            root=root,
            source=root / "source.xlsx",
            enriched=root / "data" / "enriched.csv",
            synthetic=root / "output" / "synthetic-fake-users.csv",
            email_verify=root / "data" / "email_verify_results.csv",
            leads_clean=root / "data" / "leads_clean.csv",
            xlsx=root / "output" / "gridiron-leads-enriched.xlsx",
            html=root / "output" / "enrichment-report.html",
            verification=root / "output" / "verification-report.html",
            verification_pdf=root / "output" / "verification-report.pdf",
            status=root / "status.json",
        )

    @property
    def is_ready(self) -> bool:
        return self.enriched.exists()

    @property
    def is_processing(self) -> bool:
        if not self.status.exists():
            return False
        try:
            data = json.loads(self.status.read_text())
            return data.get("current_phase") is not None and data.get("completed_at") is None
        except Exception:  # noqa: BLE001
            return False


def list_datasets() -> list[Dataset]:
    """Every uploaded dataset folder, sorted newest first.

    The legacy 'Original' (gridiron 2026-05-08) entry has been removed —
    it pointed at the 98%-synthetic source file and confused tier counts.
    To use that data, re-upload it through the sidebar so it gets a clean
    per-dataset folder.
    """
    out: list[Dataset] = []
    if DATASETS_DIR.exists():
        for d in sorted(DATASETS_DIR.iterdir(), reverse=True):
            if d.is_dir() and (d / "source.xlsx").exists():
                meta = d / "meta.json"
                label = d.name
                if meta.exists():
                    try:
                        label = json.loads(meta.read_text()).get("label", d.name)
                    except Exception:  # noqa: BLE001
                        pass
                out.append(Dataset.from_root(d.name, f"📁 {label}", d))
    return out


def get_selected_dataset() -> Dataset | None:
    """Returns the currently-selected dataset, or None if there are none."""
    datasets = list_datasets()
    if not datasets:
        return None
    name = st.session_state.get("dataset_selector", "")
    for ds in datasets:
        if ds.name == name:
            return ds
    return datasets[0]


# --- Data loaders ---------------------------------------------------------- #

def _compute_tier(row: pd.Series) -> str:
    has_li = bool(row.get("linkedin_url")) and row.get("linkedin_confidence") in (
        "high", "medium", "from_source"
    )
    has_phone = bool(row.get("phone_school") or row.get("phone_direct"))
    email_ok = row.get("email_status") in ("valid", "catch_all")
    if has_li and email_ok: return "A"
    if has_phone and email_ok: return "B"
    if email_ok: return "C"
    if has_li or has_phone: return "D"
    return "E"


def _compute_channel(row: pd.Series) -> str:
    if row.get("linkedin_url") and row.get("linkedin_confidence") in ("high", "medium", "from_source"):
        return "linkedin_dm"
    if row.get("phone_school") or row.get("phone_direct"):
        return "call"
    if row.get("email_status") in ("valid", "catch_all"):
        return "email"
    return "low_signal"


def load_enriched(ds: Dataset) -> pd.DataFrame:
    """Progressive enriched view.

    If the final `enriched.csv` exists, use it (canonical merged state).
    Otherwise build a live view by reading `leads_clean.csv` and layering on
    whatever intermediate result CSVs exist so far — so the dashboard table
    fills in row-by-row as each enrichment phase touches each lead.
    """
    if ds.enriched.exists():
        df = pd.read_csv(ds.enriched, dtype=str).fillna("")
        df["icp_score_num"] = pd.to_numeric(df["icp_score"], errors="coerce").fillna(0).astype(int)
        return df

    if not ds.leads_clean.exists():
        return pd.DataFrame()

    df = pd.read_csv(ds.leads_clean, dtype=str).fillna("")

    # Layer in email verification (as it streams to disk lead-by-lead)
    if ds.email_verify.exists():
        try:
            ev = pd.read_csv(ds.email_verify, dtype=str).fillna("")
            m = ev.set_index("lead_id")["email_status"].to_dict()
            df["email_status"] = df["lead_id"].map(m).fillna(df.get("email_status", ""))
        except Exception:  # noqa: BLE001
            pass

    # Layer in phone results
    phone_path = ds.root / "data" / "phone_results.csv"
    if phone_path.exists():
        try:
            ph = pd.read_csv(phone_path, dtype=str).fillna("")
            ms = ph.set_index("lead_id")["phone_school"].to_dict()
            md = ph.set_index("lead_id")["phone_direct"].to_dict()
            df["phone_school"] = df["lead_id"].map(ms).fillna(df.get("phone_school", ""))
            df["phone_direct"] = df["lead_id"].map(md).fillna(df.get("phone_direct", ""))
        except Exception:  # noqa: BLE001
            pass

    # Layer in LinkedIn results
    li_path = ds.root / "data" / "linkedin_results.csv"
    if li_path.exists():
        try:
            li = pd.read_csv(li_path, dtype=str).fillna("")
            mu = li.set_index("lead_id")["linkedin_url"].to_dict()
            mc = li.set_index("lead_id")["linkedin_confidence"].to_dict()
            df["linkedin_url"] = df["lead_id"].map(mu).fillna(df.get("linkedin_url", ""))
            df["linkedin_confidence"] = df["lead_id"].map(mc).fillna(df.get("linkedin_confidence", ""))
        except Exception:  # noqa: BLE001
            pass

    # Layer in domain recovery (if it ran)
    rec_path = ds.root / "data" / "domain_recovery.csv"
    if rec_path.exists():
        try:
            rc = pd.read_csv(rec_path, dtype=str).fillna("")
            high = rc[rc.get("recovery_confidence", "") == "high"]
            if not high.empty:
                new_email = high.set_index("lead_id")["new_email"].to_dict()
                new_status = high.set_index("lead_id")["new_status"].to_dict()
                df.loc[df["lead_id"].isin(new_email), "email"] = df.loc[
                    df["lead_id"].isin(new_email), "lead_id"
                ].map(new_email)
                df.loc[df["lead_id"].isin(new_status), "email_status"] = df.loc[
                    df["lead_id"].isin(new_status), "lead_id"
                ].map(new_status)
        except Exception:  # noqa: BLE001
            pass

    # Compute tier + best_channel inline from whatever data we have
    df["tier"] = df.apply(_compute_tier, axis=1)
    df["best_channel"] = df.apply(_compute_channel, axis=1)

    df["icp_score_num"] = pd.to_numeric(df.get("icp_score", "0"), errors="coerce").fillna(0).astype(int)
    return df


def load_synthetic(ds: Dataset) -> pd.DataFrame:
    if not ds.synthetic.exists():
        return pd.DataFrame()
    df = pd.read_csv(ds.synthetic, dtype=str).fillna("")
    df["dup_count_num"] = pd.to_numeric(df["dup_count"], errors="coerce").fillna(0).astype(int)
    return df


def load_domain_health(ds: Dataset) -> pd.DataFrame:
    if not ds.email_verify.exists():
        return pd.DataFrame()
    df = pd.read_csv(ds.email_verify, dtype=str).fillna("")
    df["domain"] = df["email"].str.split("@").str[1].str.lower()

    def classify(row):
        msg = (row["smtp_message"] or "").lower()
        if row["email_status"] == "invalid" and "no mx" in msg:
            return "dead_domain"
        return row["email_status"]

    df["domain_status"] = df.apply(classify, axis=1)
    return (df.groupby("domain")
              .agg(leads=("email", "count"),
                   status=("domain_status", lambda s: s.mode().iat[0] if len(s) else ""),
                   sample_email=("email", "first"),
                   sample_msg=("smtp_message", "first"))
              .reset_index()
              .sort_values(["status", "leads"], ascending=[True, False]))


def load_status(ds: Dataset) -> dict | None:
    if not ds.status.exists():
        return None
    try:
        return json.loads(ds.status.read_text())
    except Exception:  # noqa: BLE001
        return None


# --- UI helpers ------------------------------------------------------------ #

TIER_COLOR = {"A": "#dcfce7", "B": "#fef3c7", "C": "#fed7aa", "D": "#fee2e2", "E": "#e5e7eb"}
EMAIL_COLOR = {"valid": "#dcfce7", "catch_all": "#fef3c7", "invalid": "#fee2e2", "unknown": "#e5e7eb"}
LI_COLOR = {"high": "#dcfce7", "medium": "#fef3c7", "needs_review": "#fed7aa"}

PHASE_ICON = {"pending": "⏳", "running": "🔄", "done": "✅", "failed": "❌", "skipped": "⏭️"}


def pill(label: str, color: str) -> str:
    return (f'<span style="background:{color};color:#1f2937;padding:2px 10px;'
            f'border-radius:999px;font-size:12px;font-weight:600">{label}</span>')


# --- Tab renderers --------------------------------------------------------- #

def render_inline_progress(ds: Dataset) -> bool:
    """Show progress banner at top of Real Leads if a pipeline is running.
    Returns True if it rendered (so caller can decide to auto-refresh)."""
    status = load_status(ds)
    if not status:
        return False
    completed = bool(status.get("completed_at"))
    current = status.get("current_phase")
    if completed and not current:
        return False
    with st.container(border=True):
        if status.get("error"):
            st.error(f"❌ Pipeline failed at **{current}**: {status['error']}")
            return False
        cols = st.columns([3, 1])
        cols[0].markdown(f"### 🔄 Running enrichment — currently: **{current or '—'}**")
        cols[1].caption(f"Started {status.get('started_at','')[:19]}")
        # Phase strip
        phases = status.get("phases", {})
        done = sum(1 for p in phases.values() if p.get("state") == "done")
        st.progress(done / max(len(phases), 1), text=f"{done} / {len(phases)} phases done")
        # Inline checklist
        lines = []
        for key, p in phases.items():
            icon = PHASE_ICON.get(p.get("state","pending"), "•")
            lines.append(f"{icon} {p.get('label', key)}")
        st.caption(" · ".join(lines))
    return True


def render_upload_widget() -> None:
    """Compact upload form in the sidebar."""
    with st.sidebar:
        st.divider()
        st.markdown("### 📤 Upload a new dataset")
        label_input = st.text_input("Label", value=f"upload-{datetime.now():%H%M}",
                                    key="upload_label", label_visibility="collapsed",
                                    placeholder="Label (optional)")
        uploaded = st.file_uploader("Choose xlsx/csv", type=["xlsx", "csv"],
                                    key="upload_file", label_visibility="collapsed")
        if uploaded and st.button("🚀 Start enrichment", type="primary",
                                  use_container_width=True, key="start_btn"):
            _handle_upload(uploaded, label_input)


def _handle_upload(uploaded, label_input: str) -> None:
    suffix = Path(uploaded.name).suffix.lower()
    tmp_dir = PROJECT_ROOT / "_tmp_upload"
    tmp_dir.mkdir(exist_ok=True)
    tmp_file = tmp_dir / uploaded.name
    tmp_file.write_bytes(uploaded.getbuffer())

    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", label_input).strip("-") or "upload"
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"{ts}-{slug}"
    root = DATASETS_DIR / name
    root.mkdir(parents=True, exist_ok=True)

    target_xlsx = root / "source.xlsx"
    if suffix == ".csv":
        df = pd.read_csv(tmp_file, dtype=str).fillna("")
        df.to_excel(target_xlsx, index=False)
    else:
        shutil.copy(tmp_file, target_xlsx)

    (root / "meta.json").write_text(json.dumps({
        "label": label_input, "uploaded_at": datetime.now().isoformat(),
        "original_filename": uploaded.name,
    }, indent=2))

    log_path = root / "pipeline.log"
    subprocess.Popen(
        [sys.executable, str(PROJECT_ROOT / "src" / "pipeline.py"), str(root)],
        cwd=PROJECT_ROOT,
        stdout=open(log_path, "w"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    # Stash the new dataset name. main() applies it to the selectbox state
    # BEFORE the widget is rendered (Streamlit forbids writing widget state
    # after the widget exists). Then rerun for the change to take effect.
    st.session_state["pending_dataset"] = name
    st.session_state["dataset_name"] = name
    st.success(f"✅ Started — switching to **{label_input}** dataset…")
    time.sleep(1.0)
    st.rerun()


def render_real_leads(ds: Dataset) -> None:
    # Live progress banner if this dataset is currently processing
    is_processing = render_inline_progress(ds)

    df = load_enriched(ds)
    if df.empty:
        if is_processing:
            st.info("⏳ First phase still running — leads will appear here once **extract** finishes (~30 sec).")
            time.sleep(3)
            st.rerun()
        else:
            st.info("No enriched data yet for this dataset. Use **📤 Upload a new dataset** in the sidebar to start.")
        return
    st.caption(f"{len(df)} enriched leads in this dataset.")

    with st.sidebar:
        st.header("Filters — Real leads")
        tiers = st.multiselect("Tier", sorted(df["tier"].unique()),
                               default=[t for t in ["A", "B", "C"] if t in df["tier"].unique()],
                               key=f"f_tier_{ds.name}")
        channels = st.multiselect("Best channel", sorted(df["best_channel"].unique()), key=f"f_ch_{ds.name}")
        states = st.multiselect("State", sorted(df["state"].unique()), key=f"f_st_{ds.name}")
        email_st = st.multiselect("Email status", sorted(df["email_status"].unique()),
                                  default=[s for s in ["valid", "catch_all"] if s in df["email_status"].unique()],
                                  key=f"f_em_{ds.name}")
        only_li = st.checkbox("Has LinkedIn (high/medium)", value=False, key=f"f_li_{ds.name}")
        only_phone = st.checkbox("Has phone (any)", value=False, key=f"f_ph_{ds.name}")
        q = st.text_input("Search (name / school / email)", "", key=f"f_q_{ds.name}")

        st.divider()
        st.markdown("### Downloads")
        if ds.xlsx.exists():
            st.download_button("📥 Full Excel", ds.xlsx.read_bytes(),
                               file_name=ds.xlsx.name,
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        if ds.html.exists():
            st.download_button("📊 HTML report", ds.html.read_bytes(),
                               file_name=ds.html.name, mime="text/html")
        if ds.verification_pdf.exists():
            st.download_button("📑 Verification PDF (for your lead)",
                               ds.verification_pdf.read_bytes(),
                               file_name=ds.verification_pdf.name,
                               mime="application/pdf",
                               help="Print-ready PDF. Email this to your lead or attach to a Notion page.")
        if ds.verification.exists():
            st.download_button("📄 Verification (HTML, for Notion paste)",
                               ds.verification.read_bytes(),
                               file_name=ds.verification.name, mime="text/html",
                               help="Open in browser → copy contents → paste into Notion.")

    f = df.copy()
    if tiers: f = f[f["tier"].isin(tiers)]
    if channels: f = f[f["best_channel"].isin(channels)]
    if states: f = f[f["state"].isin(states)]
    if email_st: f = f[f["email_status"].isin(email_st)]
    if only_li: f = f[f["linkedin_confidence"].isin(["high", "medium"])]
    if only_phone: f = f[(f["phone_school"] != "") | (f["phone_direct"] != "")]
    if q:
        ql = q.lower()
        mask = (f["full_name"].str.lower().str.contains(ql, na=False)
                | f["school_name"].str.lower().str.contains(ql, na=False)
                | f["email"].str.lower().str.contains(ql, na=False))
        f = f[mask]

    c = st.columns(5)
    c[0].metric("Filtered", f"{len(f):,}", f"of {len(df):,}")
    c[1].metric("Tier A", int((f["tier"] == "A").sum()))
    c[2].metric("LinkedIn", int(f["linkedin_confidence"].isin(["high", "medium"]).sum()))
    c[3].metric("Phone", int(((f["phone_school"] != "") | (f["phone_direct"] != "")).sum()))
    c[4].metric("Valid email", int(f["email_status"].isin(["valid", "catch_all"]).sum()))

    st.divider()

    with st.container(border=True):
        st.markdown("### 📦 Send to Marketing")
        st.caption("Clean exports ready for HubSpot / Smartlead / Lemlist / Apollo.")
        # Build a marketing-ready frame with a single combined phone column
        market_df = df.copy()
        market_df["phone"] = market_df["phone_direct"].where(
            market_df["phone_direct"] != "", market_df["phone_school"]
        )
        cols = ["first_name", "last_name", "full_name", "role", "school_name",
                "city", "state", "email", "linkedin_url", "phone",
                "tier", "best_channel"]
        avail = [c for c in cols if c in market_df.columns]
        tier_ab = market_df[market_df["tier"].isin(["A", "B"])][avail]
        actionable = market_df[market_df["tier"].isin(["A", "B", "C"])][avail]
        c1, c2, c3 = st.columns(3)
        c1.download_button(f"⭐ Tier A+B ({len(tier_ab)})", tier_ab.to_csv(index=False).encode(),
                           file_name="marketing_tier_AB.csv", mime="text/csv", type="primary")
        c2.download_button(f"✅ All actionable ({len(actionable)})",
                           actionable.to_csv(index=False).encode(),
                           file_name="marketing_actionable.csv", mime="text/csv")
        # Current-filter download mirrors the table the user is looking at
        # (it'll include the combined `phone` column added above the table)
        filter_df = f.copy()
        if "phone" not in filter_df.columns:
            filter_df["phone"] = filter_df["phone_direct"].where(
                filter_df["phone_direct"] != "", filter_df["phone_school"]
            )
        filter_avail = [c for c in cols if c in filter_df.columns]
        c3.download_button(f"⬇️ Current filter ({len(f)})",
                           filter_df[filter_avail].to_csv(index=False).encode(),
                           file_name="marketing_filtered.csv", mime="text/csv")

    # Single phone column — prefer the coach-direct line, else the school's
    f = f.copy()
    f["phone"] = f["phone_direct"].where(f["phone_direct"] != "", f["phone_school"])

    show_cols = ["tier", "full_name", "role", "school_name", "city", "state",
                 "email", "linkedin_url", "phone", "best_channel", "icp_score"]
    show_cols = [c for c in show_cols if c in f.columns]
    f_display = f[show_cols].copy()
    f_display.columns = [c.replace("_", " ").title() for c in show_cols]
    st.dataframe(f_display, use_container_width=True, hide_index=True, height=500,
                 column_config={
                     "Email": st.column_config.LinkColumn(),
                     "Linkedin Url": st.column_config.LinkColumn("LinkedIn"),
                 })


def render_fake_users(ds: Dataset) -> None:
    df = load_synthetic(ds)
    if df.empty:
        st.info("No synthetic / fake rows detected in this dataset. Nothing to show here.")
        return
    st.warning("⚠️ These rows are flagged as fake. Do NOT outreach to them.")
    s1, s2, s3, s4 = st.columns(4)
    s1.metric("Fake rows", f"{len(df):,}")
    s2.metric("Unique emails", f"{df['email'].nunique():,}")
    s3.metric("Unique 'schools'", f"{df['company'].nunique():,}")
    s4.metric("Worst dup", f"{int(df['dup_count_num'].max())}×")

    cat_tabs = st.tabs(["🔁 Duplicate emails", "🏫 Fake school names", "💀 Dead domains", "🔍 Full search"])
    with cat_tabs[0]:
        dup_summary = (df.groupby("email")
                       .agg(dup_count=("email", "size"),
                            sample_school=("company", "first"),
                            states=("state", lambda s: ", ".join(sorted(set(s))[:5])))
                       .sort_values("dup_count", ascending=False).reset_index())
        dup_summary.columns = ["Email", "# sharing it", "Sample school", "States"]
        n = st.slider("Top N", 10, 200, 30, key=f"dup_n_{ds.name}")
        st.dataframe(dup_summary.head(n), use_container_width=True, hide_index=True, height=460)
    with cat_tabs[1]:
        sch = (df.groupby("company")
               .agg(rows=("company", "size"), unique_emails=("email", "nunique"))
               .sort_values("rows", ascending=False).reset_index())
        sch.columns = ["School (fake)", "# fake rows", "# unique emails"]
        st.dataframe(sch.head(50), use_container_width=True, hide_index=True, height=460)
    with cat_tabs[2]:
        dom = df.groupby("company_domain").size().reset_index(name="rows").sort_values("rows", ascending=False)
        dom.columns = ["Fake domain", "# fake rows"]
        st.dataframe(dom.head(80), use_container_width=True, hide_index=True, height=460)
    with cat_tabs[3]:
        q = st.text_input("Search", "", key=f"fake_q_{ds.name}")
        f = df.copy()
        if q:
            ql = q.lower()
            mask = (f["email"].str.lower().str.contains(ql, na=False)
                    | f["company"].str.lower().str.contains(ql, na=False)
                    | f["first_name"].str.lower().str.contains(ql, na=False)
                    | f["last_name"].str.lower().str.contains(ql, na=False))
            f = f[mask]
        cols = ["dup_count", "email", "first_name", "last_name", "company",
                "state", "enrichment_status", "source", "company_domain"]
        st.dataframe(f[cols], use_container_width=True, hide_index=True, height=460)


def render_domain_health(ds: Dataset) -> None:
    dh = load_domain_health(ds)
    if dh.empty:
        st.info("No domain health data yet. Run email verification first.")
        return

    real_total = int(dh["leads"].sum())
    dead = int(dh.loc[dh["status"] == "dead_domain", "leads"].sum())
    invalid = int(dh.loc[dh["status"] == "invalid", "leads"].sum())
    catch = int(dh.loc[dh["status"] == "catch_all", "leads"].sum())
    valid = int(dh.loc[dh["status"] == "valid", "leads"].sum())

    c = st.columns(5)
    c[0].metric("Total real leads", f"{real_total}")
    c[1].metric("💀 Dead domains", f"{dead}")
    c[2].metric("❌ User rejected", f"{invalid}")
    c[3].metric("⚠️ Catch-all", f"{catch}")
    c[4].metric("✅ Valid", f"{valid}")

    st.divider()
    st.markdown("### 💀 Schools with dead domains (cannot email)")
    dead_df = dh[dh["status"] == "dead_domain"].rename(columns={
        "domain": "Domain", "leads": "# leads", "status": "Status",
        "sample_email": "Sample email", "sample_msg": "DNS message"})
    st.dataframe(dead_df, use_container_width=True, hide_index=True, height=320)
    st.download_button("⬇️ Download dead-domain list",
                       dead_df.to_csv(index=False).encode(),
                       file_name=f"dead_domains_{ds.name or 'original'}.csv", mime="text/csv")


def render_progress(ds: Dataset) -> None:
    """Live progress panel for a dataset that's currently processing."""
    status = load_status(ds)
    if not status:
        st.info(
            "No pipeline has been run on this dataset yet.\n\n"
            "Go to the **📤 Upload** tab to start it."
        )
        return

    if status.get("completed_at"):
        st.success(f"✅ Pipeline completed at {status['completed_at'][:19]}")
    elif status.get("error"):
        st.error(f"❌ Pipeline failed at phase **{status['current_phase']}**: {status['error']}")
    else:
        cur = status.get("current_phase", "—")
        st.info(f"🔄 Currently running phase: **{cur}**")

    st.caption(f"Started at {status.get('started_at','')[:19]} · "
               f"updated {status.get('updated_at','')[:19]}")

    st.divider()

    # Render phase progress
    phases = status.get("phases", {})
    for key, p in phases.items():
        state = p.get("state", "pending")
        label = p.get("label", key)
        icon = PHASE_ICON.get(state, "•")
        line = f"{icon} **{label}** — {state}"
        if state == "done" and p.get("started_at") and p.get("completed_at"):
            dur = (datetime.fromisoformat(p["completed_at"]) -
                   datetime.fromisoformat(p["started_at"])).total_seconds()
            line += f" (took {dur:.0f}s)"
        st.markdown(line)
        if state in ("running", "done", "failed") and p.get("log_tail"):
            with st.expander(f"Show last log lines for {key}"):
                st.code(p["log_tail"], language="text")

    # Auto-refresh while still running
    if status.get("current_phase") and not status.get("completed_at"):
        time.sleep(3)
        st.rerun()


def render_upload() -> None:
    st.markdown("## 📤 Upload a new dataset")
    st.markdown(
        "Drop in an Excel or CSV file of leads. We'll save it as a new dataset, "
        "then you can start the full enrichment pipeline (~30–45 min). Progress "
        "shows live on the **📈 Progress** tab as each phase finishes."
    )

    label_input = st.text_input("Friendly label for this dataset",
                                value=f"upload-{datetime.now():%Y%m%d-%H%M}")
    uploaded = st.file_uploader("Choose a file", type=["xlsx", "csv"])

    if not uploaded:
        return

    # Preview the file
    suffix = Path(uploaded.name).suffix.lower()
    tmp_path = PROJECT_ROOT / "_tmp_upload"
    tmp_path.mkdir(exist_ok=True)
    tmp_file = tmp_path / uploaded.name
    tmp_file.write_bytes(uploaded.getbuffer())

    try:
        if suffix == ".csv":
            preview_df = pd.read_csv(tmp_file, dtype=str, nrows=200).fillna("")
        else:
            preview_df = pd.read_excel(tmp_file, dtype=str, nrows=200).fillna("")
    except Exception as e:  # noqa: BLE001
        st.error(f"Couldn't read file: {e}")
        return

    st.markdown(f"### Preview ({uploaded.name})")
    st.caption(f"Showing first 200 rows of {len(preview_df.columns)} columns.")
    st.dataframe(preview_df.head(20), use_container_width=True, hide_index=True)

    # Detect schema fit
    required = {"email", "first_name", "last_name", "company"}
    cols_lower = {c.lower() for c in preview_df.columns}
    missing = required - cols_lower
    if missing:
        st.warning(
            f"Missing recommended columns: **{', '.join(sorted(missing))}**. "
            f"The pipeline expects column names like `email`, `first_name`, "
            f"`last_name`, `company` (school name). Rename your file's columns "
            f"and re-upload, or proceed and accept reduced enrichment."
        )

    # Start button
    if st.button("🚀 Start enrichment", type="primary", use_container_width=True):
        slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", label_input).strip("-") or "upload"
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        name = f"{ts}-{slug}"
        root = DATASETS_DIR / name
        root.mkdir(parents=True, exist_ok=True)

        # Convert CSV uploads to xlsx so the pipeline (which expects xlsx) just works
        target_xlsx = root / "source.xlsx"
        if suffix == ".csv":
            df = pd.read_csv(tmp_file, dtype=str).fillna("")
            df.to_excel(target_xlsx, index=False)
        else:
            shutil.copy(tmp_file, target_xlsx)

        (root / "meta.json").write_text(json.dumps({
            "label": label_input,
            "uploaded_at": datetime.now().isoformat(),
            "original_filename": uploaded.name,
            "row_count_estimate": len(preview_df),
        }, indent=2))

        # Kick off the pipeline as a detached subprocess
        log_path = root / "pipeline.log"
        proc = subprocess.Popen(
            [sys.executable, str(PROJECT_ROOT / "src" / "pipeline.py"), str(root)],
            cwd=PROJECT_ROOT,
            stdout=open(log_path, "w"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

        st.session_state["dataset_name"] = name
        st.success(f"✅ Started enrichment as dataset **{name}** (PID {proc.pid}). "
                   f"Switch to the **📈 Progress** tab to watch it.")
        time.sleep(1.5)
        st.rerun()


def render_methodology() -> None:
    st.markdown("## 📋 How this dataset was built")
    st.markdown("""
**Pipeline phases** (same for every dataset, runs in sequence):

1. **Extract** — read source.xlsx, drop synthetic rows, dedupe by (school, name)
2. **Email verify** — DNS MX lookup + SMTP RCPT TO probe per address
3. **Phone discovery** — scrape each school's athletics/staff pages for phones
4. **Phone retry** — Google search "school + athletic phone" + extra URL paths + MaxPreps re-scrape
5. **LinkedIn discovery** — Google/Bing/Yahoo dorking `"Name" "School" site:linkedin.com/in`
6. **LinkedIn retry** — fallback queries + auto-promote (slug=first+last)
7. **Domain recovery** — for dead-email schools, find the real current domain, retest email
8. **Recovery filter** — drop false-positive recovered domains (must look like a school)
9. **Merge** — combine all signals, assign A/B/C/D/E tier per lead
10. **Build Excel + HTML report**

**Validation gates** prevent junk data:
- LinkedIn URL slug must contain first or last name
- Phone area code must be in the NANP allowlist
- Recovered domain must contain school keywords or be in known-good list
- Google search phones require school name in same snippet

All open-source, no paid APIs, no LinkedIn scraping.
""")


# --- Main ------------------------------------------------------------------ #

def main() -> None:
    st.set_page_config(page_title="Gridiron Leads", layout="wide", page_icon="🏈")
    st.markdown("# 🏈 Gridiron Leads — Outreach Console")

    # Apply a pending dataset switch BEFORE the selectbox widget is built —
    # Streamlit doesn't allow writes to widget state after the widget exists,
    # so the upload handler stashes the new name as `pending_dataset` and we
    # transfer it here on the next rerun.
    if "pending_dataset" in st.session_state:
        st.session_state["dataset_selector"] = st.session_state.pop("pending_dataset")

    # Dataset selector
    datasets = list_datasets()
    options = [ds.name for ds in datasets]
    labels = {ds.name: ds.label for ds in datasets}

    if not datasets:
        # First-run state — no uploads yet. Show only the upload widget and a hint.
        st.info(
            "👋 No datasets yet. Drop an .xlsx or .csv into the **📤 Upload a new dataset** "
            "widget in the left sidebar to get started. The pipeline starts automatically; "
            "this tab will fill in as each enrichment phase finishes."
        )
        render_upload_widget()
        return

    default = st.session_state.get("dataset_selector", "")
    if default not in options:
        default = options[0]

    selected = st.selectbox(
        "Dataset",
        options=options,
        index=options.index(default),
        format_func=lambda n: labels.get(n, n),
        key="dataset_selector",
    )
    st.session_state["dataset_name"] = selected
    ds = get_selected_dataset()
    if ds is None:
        render_upload_widget()
        return

    # Show status badges for the selected dataset
    cols = st.columns(4)
    cols[0].caption(f"📁 Folder: `{ds.root.name}`")
    cols[1].caption(f"📥 Source: {'✅ yes' if ds.source.exists() else '❌ missing'}")
    cols[2].caption(f"📊 Ready: {'✅' if ds.is_ready else '🚧 processing' if ds.is_processing else '⏸️ not yet'}")
    status = load_status(ds)
    if status and status.get("current_phase"):
        cols[3].caption(f"⏳ Current: **{status['current_phase']}**")
    else:
        cols[3].caption("⏳ —")

    # Sidebar upload widget — visible on every tab
    render_upload_widget()

    # Slimmer tab strip — upload + progress are now inline on Real leads
    tabs = st.tabs(["✅ Real leads", "⚠️ Fake users", "🩺 Domain health", "📋 How"])
    with tabs[0]: render_real_leads(ds)
    with tabs[1]: render_fake_users(ds)
    with tabs[2]: render_domain_health(ds)
    with tabs[3]: render_methodology()

    # If the current dataset's pipeline is still running, auto-refresh every
    # 3 sec from any tab so each tab gets the latest streaming data.
    if ds.is_processing:
        time.sleep(3)
        st.rerun()


if __name__ == "__main__":
    main()
