"""End-to-end pipeline orchestrator for a single dataset.

Each enrichment phase is a separate Python script that reads its working
directory from the GRIDIRON_DATASET_ROOT env var. We set that here, run each
phase in sequence, write a per-phase status.json so the dashboard can show
live progress, and finalize.

Usage:
    python src/pipeline.py /absolute/path/to/dataset/folder
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable

PHASES = [
    ("extract",        "src/extract.py",          "Extract clean leads"),
    ("email_verify",   "src/email_verify.py",     "Verify emails (SMTP)"),
    ("phone",          "src/phone_find.py",       "Find phone numbers"),
    ("phone_retry",    "src/phone_retry.py",      "Phone retry (Google + extras)"),
    ("linkedin",       "src/linkedin_find.py",    "Discover LinkedIn URLs"),
    ("linkedin_retry", "src/linkedin_retry.py",   "LinkedIn retry passes"),
    ("domain_recovery","src/domain_recovery.py",  "Recover dead email domains"),
    ("recovery_filter","src/recovery_filter.py",  "Filter recovery false positives"),
    ("merge",          "src/merge.py",            "Merge all signals"),
    ("build_xlsx",     "src/build_xlsx.py",       "Build Excel workbook"),
    ("build_report",   "src/build_report.py",     "Build HTML report"),
    ("verification",   "src/build_verification.py","Build shareable verification report"),
    ("add_synthetic",  "src/add_synthetic_tabs.py","Add fake-data tabs (if synthetic data present)"),
]


def write_status(status_path: Path, state: dict) -> None:
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    tmp = status_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(status_path)


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: pipeline.py <dataset_root_path>", file=sys.stderr)
        return 2

    root = Path(sys.argv[1]).resolve()
    if not root.exists():
        print(f"dataset root does not exist: {root}", file=sys.stderr)
        return 3
    src_xlsx = root / "source.xlsx"
    if not src_xlsx.exists():
        print(f"source.xlsx missing in {root}", file=sys.stderr)
        return 4

    logs_dir = root / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    status_path = root / "status.json"

    state: dict = {
        "dataset_root": str(root),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "completed_at": None,
        "current_phase": None,
        "phases": {
            key: {"label": label, "state": "pending", "started_at": None,
                  "completed_at": None, "log_tail": ""}
            for key, _script, label in PHASES
        },
    }
    write_status(status_path, state)

    env = {**os.environ, "GRIDIRON_DATASET_ROOT": str(root)}

    for key, script, label in PHASES:
        log_path = logs_dir / f"{key}.log"
        state["current_phase"] = key
        state["phases"][key]["state"] = "running"
        state["phases"][key]["started_at"] = datetime.now(timezone.utc).isoformat()
        write_status(status_path, state)

        print(f"\n=== Phase: {label} ({script}) ===", flush=True)
        with log_path.open("w") as logf:
            proc = subprocess.run(
                [PY, str(PROJECT_ROOT / script)],
                cwd=PROJECT_ROOT,
                env=env,
                stdout=logf,
                stderr=subprocess.STDOUT,
                check=False,
            )

        try:
            tail = "\n".join(log_path.read_text().splitlines()[-20:])
        except Exception:  # noqa: BLE001
            tail = ""
        state["phases"][key]["log_tail"] = tail
        state["phases"][key]["completed_at"] = datetime.now(timezone.utc).isoformat()

        if proc.returncode != 0:
            # add_synthetic is optional — many uploads won't have synthetic data
            if key == "add_synthetic":
                state["phases"][key]["state"] = "skipped"
                state["phases"][key]["log_tail"] += "\n(skipped — no synthetic rows in source)"
                write_status(status_path, state)
                continue
            state["phases"][key]["state"] = "failed"
            state["current_phase"] = None
            state["completed_at"] = datetime.now(timezone.utc).isoformat()
            state["error"] = f"{key} exited with code {proc.returncode}"
            write_status(status_path, state)
            print(f"FAILED at phase: {key}", flush=True)
            return proc.returncode

        state["phases"][key]["state"] = "done"
        write_status(status_path, state)

    state["current_phase"] = None
    state["completed_at"] = datetime.now(timezone.utc).isoformat()
    write_status(status_path, state)
    print("\n=== Pipeline complete ===", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
