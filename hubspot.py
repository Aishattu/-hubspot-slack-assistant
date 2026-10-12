"""Thin HubSpot CRM client: the only file that talks to the HubSpot API.

Auth is one bearer token (HUBSPOT_TOKEN in .env). Works with a legacy Private App token or a Service Key.
Every function returns plain dicts/lists that are safe to json.dumps() back to Claude.
"""
import os
import time

import requests

BASE = "https://api.hubapi.com"
OBJECT_TYPES = ("contacts", "companies", "deals", "tickets")
ACTIVITY_TYPES = ("notes", "tasks")
# Object type IDs used in record URLs (app.hubspot.com/contacts/<portal>/record/<id>/<record>)
TYPE_IDS = {"contacts": "0-1", "companies": "0-2", "deals": "0-3", "tickets": "0-5"}
# Properties shown when Claude doesn't ask for specific ones
DEFAULT_PROPS = {
    "contacts": ["firstname", "lastname", "email", "jobtitle", "company", "phone", "lifecyclestage",
                 "hs_lead_status", "hubspot_owner_id", "createdate", "hs_object_source_label"],
    "companies": ["name", "domain", "industry", "city", "country", "numberofemployees", "annualrevenue",
                  "lifecyclestage", "hubspot_owner_id", "createdate", "hs_object_source_label"],
    "deals": ["dealname", "amount", "dealstage", "pipeline", "closedate", "hubspot_owner_id",
              "createdate", "hs_lastmodifieddate"],
    "tickets": ["subject", "content", "hs_pipeline", "hs_pipeline_stage", "hs_ticket_priority",
                "hubspot_owner_id", "hs_lastmodifieddate"],
}

_session = requests.Session()
_cache = {}  # property lists, pipelines, portal id: rarely change, so fetch once per run


class HubSpotError(Exception):
    pass


def _request(method, path, **kw):
    _session.headers["Authorization"] = f"Bearer {os.environ['HUBSPOT_TOKEN']}"
    for attempt in range(4):
        resp = _session.request(method, BASE + path, timeout=30, **kw)
        if resp.status_code == 429 and attempt < 3:  # rate limited: back off and retry
            time.sleep(int(resp.headers.get("Retry-After", 2 ** attempt)))
            continue
        break
    if resp.status_code >= 400:
        try:
            msg = resp.json().get("message", resp.text)
        except ValueError:
            msg = resp.text
        raise HubSpotError(f"HubSpot {resp.status_code}: {msg[:500]}")
    return resp.json() if resp.content else {}


def _check_type(object_type, allowed=OBJECT_TYPES):
    if object_type not in allowed:
        raise HubSpotError(f"Unknown object type '{object_type}'. Use one of: {', '.join(allowed)}")


def portal_id():
    if "portal" not in _cache:
        _cache["portal"] = _request("GET", "/account-info/v3/details").get("portalId")
    return _cache["portal"]


def record_url(object_type, record_id):
    try:
        return f"https://app.hubspot.com/contacts/{portal_id()}/record/{TYPE_IDS[object_type]}/{record_id}"
    except (HubSpotError, KeyError):
        return None


def _shape(object_type, obj):
    return {"id": obj["id"], "url": record_url(object_type, obj["id"]),
            "properties": {k: v for k, v in obj.get("properties", {}).items() if v not in (None, "")}}


# ---------- read ----------

def search_records(object_type, query=None, filters=None, properties=None, sort_by=None,
                   sort_direction="DESCENDING", limit=10):
    """filters: [{"property": "dealstage", "operator": "EQ", "value": "closedwon"}, ...] (all must match)."""
    _check_type(object_type)
    body = {"limit": max(1, min(int(limit or 10), 100)),
            "properties": properties or DEFAULT_PROPS[object_type]}
    if query:
        body["query"] = query
    if filters:
        body["filterGroups"] = [{"filters": [
            {"propertyName": f["property"], "operator": f.get("operator", "EQ"),
             **({"values": f["values"]} if "values" in f else {}),
             **({"value": str(f["value"])} if "value" in f else {})}
            for f in filters]}]
    if sort_by:
        body["sorts"] = [{"propertyName": sort_by, "direction": sort_direction}]
    data = _request("POST", f"/crm/v3/objects/{object_type}/search", json=body)
    return {"total": data.get("total", 0),
            "results": [_shape(object_type, r) for r in data.get("results", [])]}


def get_record(object_type, record_id, properties=None):
    _check_type(object_type)
    params = {"properties": ",".join(properties or DEFAULT_PROPS[object_type])}
    return _shape(object_type, _request("GET", f"/crm/v3/objects/{object_type}/{record_id}", params=params))


def get_associations(object_type, record_id, to_object_type, limit=25):
    """Records linked to this one, e.g. the contacts on a deal. Returns their key properties too."""
    _check_type(object_type)
    _check_type(to_object_type, OBJECT_TYPES + ACTIVITY_TYPES)
    data = _request("GET", f"/crm/v4/objects/{object_type}/{record_id}/associations/{to_object_type}",
                    params={"limit": 500})
    ids = [str(r["toObjectId"]) for r in data.get("results", [])]
    total, ids = len(ids), ids[:limit]
    if not ids:
        return {"total": 0, "results": []}
    props = DEFAULT_PROPS.get(to_object_type) or (
        ["hs_note_body", "hs_timestamp"] if to_object_type == "notes"
        else ["hs_task_subject", "hs_task_status", "hs_timestamp", "hubspot_owner_id"])
    batch = _request("POST", f"/crm/v3/objects/{to_object_type}/batch/read",
                     json={"properties": props, "inputs": [{"id": i} for i in ids]})
    return {"total": total, "results": [_shape(to_object_type, r) for r in batch.get("results", [])]}


def list_properties(object_type, search=None):
    """Field names, labels, types and allowed values. Use to map 'Deal Stage' → dealstage etc."""
    _check_type(object_type)
    key = ("props", object_type)
    if key not in _cache:
        _cache[key] = _request("GET", f"/crm/v3/properties/{object_type}").get("results", [])
    out = []
    for p in _cache[key]:
        if p.get("hidden"):
            continue
        if search and search.lower() not in (p["name"] + " " + p.get("label", "")).lower():
            continue
        item = {"name": p["name"], "label": p.get("label"), "type": p.get("type"),
                "read_only": p.get("modificationMetadata", {}).get("readOnlyValue", False)}
        if p.get("options"):
            item["options"] = [{"value": o["value"], "label": o["label"]} for o in p["options"][:50]]
        out.append(item)
    return out[:80] if search else [{"name": i["name"], "label": i["label"]} for i in out][:300]


def list_pipelines(object_type):
    """Pipelines and stage IDs for deals or tickets (stage IDs are what HubSpot stores)."""
    _check_type(object_type, ("deals", "tickets"))
    key = ("pipelines", object_type)
    if key not in _cache:
        data = _request("GET", f"/crm/v3/pipelines/{object_type}")
        _cache[key] = [{"id": p["id"], "label": p["label"],
                        "stages": [{"id": s["id"], "label": s["label"]} for s in p.get("stages", [])]}
                       for p in data.get("results", [])]
    return _cache[key]


def list_owners(email=None):
    """HubSpot users who can own records (hubspot_owner_id)."""
    params = {"limit": 100, **({"email": email} if email else {})}
    data = _request("GET", "/crm/v3/owners", params=params)
    return [{"id": o["id"], "name": f"{o.get('firstName', '')} {o.get('lastName', '')}".strip(),
             "email": o.get("email")} for o in data.get("results", [])]


# ---------- write (only ever called after a human clicks Approve in Slack) ----------

def update_record(object_type, record_id, properties):
    _check_type(object_type)
    return _shape(object_type, _request("PATCH", f"/crm/v3/objects/{object_type}/{record_id}",
                                        json={"properties": properties}))


CREATABLE = ("contacts", "deals")


def create_record(object_type, properties, associate=None):
    """Create a contact or deal. associate: [{"object_type": "companies", "record_id": "123"}, ...]"""
    _check_type(object_type, CREATABLE)
    obj = _request("POST", f"/crm/v3/objects/{object_type}", json={"properties": properties})
    for a in associate or []:
        _associate(object_type, obj["id"], a["object_type"], a["record_id"])
    return _shape(object_type, obj)


def _now_ms():
    return str(int(time.time() * 1000))


def _associate(from_type, from_id, to_type, to_id):
    _request("PUT", f"/crm/v4/objects/{from_type}/{from_id}/associations/default/{to_type}/{to_id}")


def create_note(object_type, record_id, body):
    _check_type(object_type)
    note = _request("POST", "/crm/v3/objects/notes",
                    json={"properties": {"hs_note_body": body, "hs_timestamp": _now_ms()}})
    _associate("notes", note["id"], object_type, record_id)
    return {"note_id": note["id"], "attached_to": record_url(object_type, record_id)}


def create_task(object_type, record_id, subject, body=None, due_date_ms=None, owner_id=None, priority=None):
    _check_type(object_type)
    props = {"hs_task_subject": subject, "hs_task_status": "NOT_STARTED",
             "hs_timestamp": str(due_date_ms or _now_ms())}
    if body:
        props["hs_task_body"] = body
    if owner_id:
        props["hubspot_owner_id"] = str(owner_id)
    if priority:
        props["hs_task_priority"] = priority
    task = _request("POST", "/crm/v3/objects/tasks", json={"properties": props})
    _associate("tasks", task["id"], object_type, record_id)
    return {"task_id": task["id"], "attached_to": record_url(object_type, record_id)}
