#!/usr/bin/env bash
# Local copy launcher — access at http://localhost:8501
set -euo pipefail
cd "$(dirname "$0")"
exec ~/.local/bin/uv run streamlit run src/app.py \
  --server.address 0.0.0.0 \
  --server.port 8501 \
  --server.headless true \
  --server.maxUploadSize 200 \
  --server.enableXsrfProtection false \
  --browser.serverAddress localhost \
  --browser.gatherUsageStats false
