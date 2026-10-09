#!/bin/sh
# Start the HubSpot Assistant. First run creates the virtualenv and installs the packages.
cd "$(dirname "$0")"
[ -d .venv ] || { python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt; }
exec .venv/bin/python -u bot.py
