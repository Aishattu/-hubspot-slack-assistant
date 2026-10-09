#!/usr/bin/env python3
"""Check every key in .env works before starting the bot. Read-only: changes nothing anywhere.

Run: .venv/bin/python check_setup.py
"""
import os
from pathlib import Path

for line in (Path(__file__).parent / ".env").read_text().splitlines():
    if "=" in line and not line.lstrip().startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

ok = True


def check(name, fn):
    global ok
    try:
        print(f"✅ {name}: {fn()}")
    except Exception as e:
        ok = False
        print(f"❌ {name}: {type(e).__name__}: {str(e)[:300]}")


def slack():
    from slack_sdk import WebClient
    r = WebClient(token=os.environ["SLACK_BOT_TOKEN"]).auth_test()
    assert os.environ.get("SLACK_APP_TOKEN", "").startswith("xapp-"), "SLACK_APP_TOKEN missing or not an xapp- token"
    return f"bot '{r['user']}' in workspace '{r['team']}'"


def hubspot_read():
    import hubspot
    out = []
    for t in hubspot.OBJECT_TYPES:
        try:
            out.append(f"{t} ✓ ({hubspot.search_records(t, limit=1)['total']})")
        except hubspot.HubSpotError as e:
            out.append(f"{t} ✗ ({str(e)[:80]})")
    out.append(f"properties {'✓' if hubspot.list_properties('deals') else '✗'}")
    out.append(f"owners ✓ ({len(hubspot.list_owners())})")
    if "✗" in out[3]:  # tickets are optional: many portals don't have Service Hub
        out[3] = "tickets ⚠️ not available (fine if you don't use tickets)"
    if any("✗" in o for o in out):
        raise RuntimeError(" · ".join(out) + "  → add the missing scopes to the HubSpot key")
    return " · ".join(out)


def claude():
    import anthropic
    r = anthropic.Anthropic().messages.create(model="claude-opus-5-5", max_tokens=200,
                                              output_config={"effort": "low"},
                                              messages=[{"role": "user", "content": "Reply with just: ok"}])
    return f"{r.model} answered (cost: well under $0.01)"


check("Slack", slack)
check("HubSpot (read)", hubspot_read)
check("Claude", claude)
print("\nAll good. Start the bot with: sh run.sh" if ok else "\nFix the ❌ items above, then run this again.")
