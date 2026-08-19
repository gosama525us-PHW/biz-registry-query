#!/usr/bin/env bash
set -euo pipefail

python -m pip install \
  --retries 10 \
  --timeout 120 \
  -r requirements.txt

python -m playwright install chromium
python install_cjk_font.py
