#!/usr/bin/env bash
set -euo pipefail

pip install -r requirements.txt
python -m playwright install chromium
python install_cjk_font.py
