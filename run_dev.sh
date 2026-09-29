#!/bin/bash
# Jalankan langsung tanpa build (mode pengembangan)
set -e
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
source .venv/bin/activate
pip install -q -r requirements.txt
python run_app.py
