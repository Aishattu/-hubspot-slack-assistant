"""The HubSpot assistant brain: Claude + HubSpot tools.

Read tools run straight away. Write tools (update_record, create_note, create_task) never touch HubSpot here:
they come back as "proposals" that bot.py posts to Slack with Approve / Cancel buttons. HubSpot only changes
when a person clicks Approve.
"""
import json
import os
import uuid
from datetime import datetime, timezone

import anthropic

import hubspot

MODEL = "claude-opus-5-5"
MAX_STEPS = 12  # tool rounds per question before giving up (stops runaway loops/costs)
client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"), max_retries=3, timeout=120)

SYSTEM = """You are the HubSpot assistant for a sales/GTM team, answering inside Slack.

How to work:
- Use the tools to look things up. Never invent records, IDs, numbers or field values. If you can't find something, say so.
- HubSpot stores internal names, not labels. Before filtering or updating a field you haven't seen yet, call list_properties (with a search term) or list_pipelines to get the exact property name and option/stage IDs.
- Deal stages and ticket stages are IDs that belong to a pipeline. Translate them to their labels when you answer.
- Owners are IDs (hubspot_owner_id). Use list_owners to turn them into names, or to find someone's ID.
- If a search returns several possible matches for a change, ask which one the user means before proposing anything.
- Read questions the way a teammate would. "The last contact I created" or "my latest deal" usually means the newest one in the CRM, not only records whose creator is the user's own HubSpot login (many are created by imports, forms and integrations). Answer the broad reading first, and mention the narrower one if it gives a different answer.
- Whenever a filter you chose leaves records out, say so in one line (e.g. "13 more were created by integrations"). Never quietly narrow the question.
- For "newest/latest/last created" use sort_by createdate DESCENDING. Use hs_object_source_label to say where a record came from (import, integration, form, manual).

Changing data:
- update_record, create_note and create_task do NOT change HubSpot directly. They create a proposal that is shown to the user with Approve / Cancel buttons. Only after someone clicks Approve is HubSpot updated.
- So after proposing, say what you proposed and that it's waiting for approval. Never say it's done.
- Only propose changes the user actually asked for. Look up the record first so you have the right ID and the current value.

Answer style (this is Slack):
- Short and scannable. Lead with the answer. Use bullets for lists, *bold* for names. Slack uses *single asterisks* for bold, not double.
- Link records with Slack link syntax: <url|Record name>.
- Format money and dates in a human way (e.g. $12,500 · 14 Oct 2026).
- For long lists, show the top results and say how many there are in total."""

OBJ = {"type": "string", "enum": list(hubspot.OBJECT_TYPES)}
PROPS = {"type": "array", "items": {"type": "string"},
         "description": "Internal property names to return. Leave out to get the usual key fields."}

TOOLS = [
    {"name": "search_records",
     "description": "Search contacts, companies, deals or tickets. Use `query` for free text (name, email, domain) "
                    "and/or `filters` for exact conditions. All filters must match. Operators: EQ, NEQ, LT, LTE, GT, GTE, "
                    "BETWEEN, IN, NOT_IN, HAS_PROPERTY, NOT_HAS_PROPERTY, CONTAINS_TOKEN. Dates are millisecond timestamps "
                    "or YYYY-MM-DD.",
     "input_schema": {"type": "object", "properties": {
         "object_type": OBJ,
         "query": {"type": "string"},
         "filters": {"type": "array", "items": {"type": "object", "properties": {
             "property": {"type": "string"}, "operator": {"type": "string"},
             "value": {"type": "string"}, "values": {"type": "array", "items": {"type": "string"}}},
             "required": ["property", "operator"]}},
         "properties": PROPS,
         "sort_by": {"type": "string", "description": "Property to sort by, e.g. amount or createdate"},
         "sort_direction": {"type": "string", "enum": ["ASCENDING", "DESCENDING"]},
         "limit": {"type": "integer", "description": "1-100, default 10"}},
         "required": ["object_type"]}},
    {"name": "get_record",
     "description": "Get one record by ID with the properties you need.",
     "input_schema": {"type": "object", "properties": {
         "object_type": OBJ, "record_id": {"type": "string"}, "properties": PROPS},
         "required": ["object_type", "record_id"]}},
    {"name": "get_associations",
     "description": "Records linked to a record, e.g. contacts on a deal, deals of a company, notes or tasks on a contact.",
     "input_schema": {"type": "object", "properties": {
         "object_type": OBJ, "record_id": {"type": "string"},
         "to_object_type": {"type": "string", "enum": list(hubspot.OBJECT_TYPES + hubspot.ACTIVITY_TYPES)}},
         "required": ["object_type", "record_id", "to_object_type"]}},
    {"name": "list_properties",
     "description": "Find property internal names, types and allowed values for an object type. "
                    "Pass `search` (e.g. 'lead status') to get full details for matching fields.",
     "input_schema": {"type": "object", "properties": {"object_type": OBJ, "search": {"type": "string"}},
                      "required": ["object_type"]}},
    {"name": "list_pipelines",
     "description": "Pipelines and their stage IDs/labels for deals or tickets.",
     "input_schema": {"type": "object", "properties": {"object_type": {"type": "string", "enum": ["deals", "tickets"]}},
                      "required": ["object_type"]}},
    {"name": "list_owners",
     "description": "HubSpot users who can own records, with their owner IDs. Optionally filter by email.",
     "input_schema": {"type": "object", "properties": {"email": {"type": "string"}}}},
    {"name": "update_record",
     "description": "PROPOSE changing properties on a record. Shown to the user for approval; does not change HubSpot by itself.",
     "input_schema": {"type": "object", "properties": {
         "object_type": OBJ, "record_id": {"type": "string"},
         "record_name": {"type": "string", "description": "Human name of the record, for the approval message"},
         "properties": {"type": "object", "additionalProperties": {"type": "string"},
                        "description": "internal_name → new value"},
         "current_values": {"type": "object", "additionalProperties": {"type": "string"},
                            "description": "internal_name → value before the change (human-readable)"},
         "summary": {"type": "string", "description": "One plain-English line describing the change"}},
         "required": ["object_type", "record_id", "record_name", "properties", "summary"]}},
    {"name": "create_note",
     "description": "PROPOSE adding a note to a record. Shown to the user for approval first.",
     "input_schema": {"type": "object", "properties": {
         "object_type": OBJ, "record_id": {"type": "string"}, "record_name": {"type": "string"},
         "body": {"type": "string"}},
         "required": ["object_type", "record_id", "record_name", "body"]}},
    {"name": "create_task",
     "description": "PROPOSE creating a task on a record. Shown to the user for approval first.",
     "input_schema": {"type": "object", "properties": {
         "object_type": OBJ, "record_id": {"type": "string"}, "record_name": {"type": "string"},
         "subject": {"type": "string"}, "body": {"type": "string"},
         "due_date": {"type": "string", "description": "YYYY-MM-DD"},
         "owner_id": {"type": "string"},
         "priority": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH"]}},
         "required": ["object_type", "record_id", "record_name", "subject"]}},
]

READ_TOOLS = {
    "search_records": hubspot.search_records,
    "get_record": hubspot.get_record,
    "get_associations": hubspot.get_associations,
    "list_properties": hubspot.list_properties,
    "list_pipelines": hubspot.list_pipelines,
    "list_owners": hubspot.list_owners,
}
WRITE_TOOLS = {"update_record", "create_note", "create_task"}


def _due_ms(date_str):
    d = datetime.strptime(date_str, "%Y-%m-%d").replace(hour=9, tzinfo=timezone.utc)
    return int(d.timestamp() * 1000)


def make_proposal(name, args):
    """Turn a write-tool call into a pending change that a human must approve."""
    p = {"id": uuid.uuid4().hex[:10], "tool": name, "object_type": args["object_type"],
         "record_id": str(args["record_id"]), "record_name": args.get("record_name", args["record_id"])}
    if name == "update_record":
        p.update(properties={k: str(v) for k, v in args["properties"].items()},
                 current_values=args.get("current_values") or {}, summary=args.get("summary", ""))
    elif name == "create_note":
        p.update(body=args["body"])
    else:
        p.update(subject=args["subject"], body=args.get("body"), owner_id=args.get("owner_id"),
                 priority=args.get("priority"),
                 due_date=args.get("due_date"),
                 due_date_ms=_due_ms(args["due_date"]) if args.get("due_date") else None)
    return p


def apply_proposal(p):
    """Called by bot.py when someone clicks Approve."""
    if p["tool"] == "update_record":
        return hubspot.update_record(p["object_type"], p["record_id"], p["properties"])
    if p["tool"] == "create_note":
        return hubspot.create_note(p["object_type"], p["record_id"], p["body"])
    return hubspot.create_task(p["object_type"], p["record_id"], p["subject"], p.get("body"),
                               p.get("due_date_ms"), p.get("owner_id"), p.get("priority"))


def _run_tool(name, args, can_write, proposals):
    print(f"  tool {name} {json.dumps(args)[:300]}", flush=True)  # shows in bot.log
    try:
        if name in READ_TOOLS:
            return json.dumps(READ_TOOLS[name](**args), default=str)[:60000], False
        if name in WRITE_TOOLS:
            if not can_write:
                return ("This Slack user is not allowed to change HubSpot records. Tell them to ask an admin "
                        "to add them to the allowed list."), True
            p = make_proposal(name, args)
            proposals.append(p)
            return (f"Proposal {p['id']} created. The user will see Approve / Cancel buttons. "
                    "HubSpot has NOT been changed yet."), False
        return f"Unknown tool {name}", True
    except (hubspot.HubSpotError, KeyError, TypeError, ValueError) as e:
        return f"Error: {e}", True


def _to_param(block):
    return block.model_dump(mode="json", exclude_none=True)


def ask(history, user_text, can_write=True):
    """Run one Slack question through Claude. `history` is this thread's message list (appended to in place).

    Returns (reply_text, proposals).
    """
    history.append({"role": "user", "content": user_text})
    proposals = []
    for _ in range(MAX_STEPS):
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            system=SYSTEM,
            tools=TOOLS,
            messages=history,
            output_config={"effort": "medium"},
            cache_control={"type": "ephemeral"},
            # If Claude's safety filter wrongly declines a request, retry it on Anthropic's recommended backup model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        history.append({"role": "assistant", "content": [_to_param(b) for b in response.content]})

        if response.stop_reason == "refusal":
            return "Sorry, I can't help with that one.", proposals
        tool_calls = [b for b in response.content if b.type == "tool_use"]
        if response.stop_reason != "tool_use" or not tool_calls:
            text = "\n".join(b.text for b in response.content if b.type == "text").strip()
            if response.stop_reason == "max_tokens":
                text += "\n_(answer cut off: too long)_"
            return text or "Done.", proposals

        results = []
        for call in tool_calls:
            output, is_error = _run_tool(call.name, dict(call.input or {}), can_write, proposals)
            results.append({"type": "tool_result", "tool_use_id": call.id, "content": output,
                            **({"is_error": True} if is_error else {})})
        history.append({"role": "user", "content": results})

    return ("I took too many steps on this one and stopped. Try asking more specifically "
            "(e.g. include the company or deal name)."), proposals
