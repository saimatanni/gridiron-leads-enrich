"""Run the post-enrichment finalize chain: merge -> xlsx -> html report.

Each step is idempotent and re-runs cleanly.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


def run(label: str, script: str) -> None:
    print(f"\n=== {label} ===")
    r = subprocess.run([PY, str(ROOT / "src" / script)], cwd=ROOT, check=False)
    if r.returncode != 0:
        sys.exit(f"FAILED: {script}")


if __name__ == "__main__":
    run("merge enrichment data", "merge.py")
    run("build Excel workbook", "build_xlsx.py")
    run("build HTML report", "build_report.py")
    print("\nAll outputs ready in:", ROOT / "output")
