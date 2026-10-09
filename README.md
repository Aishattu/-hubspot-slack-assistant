# HubSpot Assistant for Slack

An AI agent that lets your team ask HubSpot questions and update HubSpot records from Slack, in plain English.

> **@HubSpot Assistant** what stage is the Acme deal in and who owns it?
>
> **@HubSpot Assistant** move the Acme deal to Closed Won and set the amount to 25,000

It looks things up in HubSpot and answers in the Slack thread with links to the records. When someone asks for a change, it **never edits HubSpot straight away**. It posts a card showing exactly what will change, with **Approve** and **Cancel** buttons. HubSpot is only updated when a person clicks Approve.

## What it can do

**Read**
- Search contacts, companies, deals and tickets by name, email, domain, owner, stage, date or any other field
- Open any record and show its key details
- Show linked records (contacts on a deal, deals of a company, notes and tasks on a contact)
- Translate HubSpot's internal values into readable ones (stage IDs into stage names, owner IDs into people)
- Understand "my deals" by matching the person's Slack email to their HubSpot owner

**Write (always with approval)**
- Update any field on a record
- Add a note to a record
- Create a task on a record (with due date, owner and priority)

**Conversation**
- Answers in a thread; follow-up replies in that thread don't need an @mention
- Works in channels (with an @mention) and in direct messages

## How it works

```
Slack message
   │
   ▼
bot.py ──────► agent.py (Claude) ──────► hubspot.py ──────► HubSpot API
   ▲              │  decides which          read tools run
   │              │  tools to call          immediately
   │              ▼
   │         write tools only create a "proposal"
   │              │
   └── posts the answer + an Approve / Cancel card
              │
         someone clicks Approve ──► change is saved to HubSpot ──► logged in audit.jsonl
```

1. **bot.py** connects to Slack over Socket Mode. The bot opens the connection to Slack, so it doesn't need a public web address. It receives the message, shows 👀 while working, and replies in the thread.
2. **agent.py** sends the question to Claude (Opus 5.5) together with a set of HubSpot tools. Claude decides which tools to call and in what order. For example, it looks up the pipeline stages first, then searches deals with the right stage ID. It then writes the answer in Slack format.
3. **hubspot.py** makes the actual HubSpot API calls. It retries automatically if HubSpot rate-limits it.
4. **Write tools are fenced off.** When Claude calls `update_record`, `create_note` or `create_task`, nothing is sent to HubSpot. The request becomes a proposal card in Slack, and only the Approve button calls HubSpot.

## Safety

| Risk | How it's handled |
|---|---|
| AI changes the wrong data | Every change needs a human to click Approve, and the card shows old value → new value |
| Wrong person changes data | `ALLOWED_WRITERS` limits who can propose and approve changes (everyone can still read) |
| Double-click applies a change twice | Each proposal can only be used once |
| No record of who changed what | Every approved, cancelled or failed change is written to `audit.jsonl` |
| AI makes up an answer | The agent is told to only state what the tools return, and to say when a filter left records out |
| Runaway loops or cost | Capped at 12 tool rounds per question |

## Files

| File | Purpose |
|---|---|
| `bot.py` | Slack listener, thread replies, Approve/Cancel cards, audit log |
| `agent.py` | Claude instructions, tool definitions, the agent loop |
| `hubspot.py` | All HubSpot API calls |
| `check_setup.py` | Tests every key without changing anything |
| `slack-app-manifest.json` | Creates the Slack app with the right permissions in one paste |
| `run.sh` / `install.sh` | Run in the terminal / run as a background service on macOS |
| `.env.example` | Template for the keys (the real `.env` is never committed) |

## Setup

**You need:** Python 3.9+, a Slack workspace where you can install apps, a HubSpot account (super admin to create the key), and an Anthropic API key with credit.

**1. HubSpot key.** Go to Settings → Integrations → **Service Keys** (or a legacy Private App token if you already have one). Give it read and write access to contacts, companies and deals; read access to owners and to the contact, company and deal schemas; and access to notes and tasks. Add tickets only if you use them.

**2. Slack app.** At [api.slack.com/apps](https://api.slack.com/apps) choose **Create New App → From a manifest**, then paste the *contents* of `slack-app-manifest.json`. Then:
- Basic Information → App-Level Tokens → generate one with `connections:write` (this is the `xapp-` token)
- Install App → Install to Workspace (this gives the `xoxb-` token)

**3. Anthropic key.** Create one at [console.anthropic.com](https://console.anthropic.com).

**4. Configure and run**
```sh
git clone <this repo> && cd hubspot-slack-assistant
cp .env.example .env              # paste your 4 keys into .env
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python check_setup.py   # every line should show ✅
sh run.sh                         # or: sh install.sh to run in the background on macOS
```

**5. Use it.** DM the bot, or `/invite @HubSpot Assistant` to a channel and @mention it.

## Good to know

- **It must stay running to answer.** Socket Mode keeps a live connection to Slack. If the machine sleeps, the bot goes quiet until it wakes up. For 24/7 use, host it on an always-on server (Railway, Render, a VPS). It runs there unchanged.
- **Thread memory resets on restart.** Pending approval cards and the audit log are saved to disk and survive.
- **Cost.** Each question costs a few cents of Claude usage, depending on how many lookups it needs.
- **HubSpot Private Apps.** HubSpot is ending creation of new legacy Private Apps (from 26 Oct 2026 for existing accounts). Existing tokens keep working. New setups should use Service Keys.
- **Tickets** need Service Hub. Without it, the bot just says it can't see tickets.

## Built with

[Slack Bolt for Python](https://github.com/slackapi/bolt-python) · [Anthropic Python SDK](https://github.com/anthropics/anthropic-sdk-python) (Claude Opus 5.5, tool use) · [HubSpot CRM API v3/v4](https://developers.hubspot.com/docs/api/crm/understanding-the-crm)
