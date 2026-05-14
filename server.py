"""MCP server for Pipedrive — YvY Capital.

Exposes deals, contacts (persons + organizations), activities, custom-field
schema management, and an automatic backup system for undo.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

load_dotenv()

API_TOKEN = os.environ["PIPEDRIVE_API_TOKEN"]
DOMAIN = os.environ["PIPEDRIVE_DOMAIN"]
BASE_URL = f"https://{DOMAIN}.pipedrive.com/api/v2"
BASE_URL_V1 = f"https://{DOMAIN}.pipedrive.com/api/v1"

# YvY default — Renda Fixa pipeline
DEFAULT_PIPELINE_ID = 6

# Fields que NUNCA podem ser removidos do schema nem ter o valor limpo num deal.
# Cobrem o que faz o deal existir minimamente (nome, valor, vínculos, etapa).
PROTECTED_FIELD_KEYS = {
    "value",         # valor do deal
    "currency",      # moeda
    "title",         # nome do deal
    "org_id",        # empresa vinculada
    "person_id",     # contato vinculado
    "stage_id",      # etapa do funil
    "pipeline_id",   # funil
    "status",        # open/won/lost
    "id", "add_time", "update_time", "creator_user_id",
}

BACKUPS_DIR = Path(__file__).parent / "backups"
BACKUPS_DIR.mkdir(exist_ok=True)

# DNS-rebinding protection desligada porque já temos bearer auth gateando /mcp.
# Senão o FastMCP rejeita Host headers que não sejam localhost com 421 "Invalid Host header".
mcp = FastMCP(
    "pipedrive-yvy",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


# ===== HTTP =====

async def _request(method: str, path: str, *, v1: bool = False, **kwargs: Any) -> dict[str, Any]:
    base = BASE_URL_V1 if v1 else BASE_URL
    params = kwargs.pop("params", {}) or {}
    params["api_token"] = API_TOKEN
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.request(method, f"{base}{path}", params=params, **kwargs)
        r.raise_for_status()
        return r.json()


# ===== Backup system =====

def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _write_snapshot(
    operation: str,
    entity_type: str,
    entity_id: int | str | None,
    before: dict | None,
    params: dict | None,
    after: dict | None = None,
) -> str:
    fname = f"{_ts()}_{operation}_{entity_type}_{entity_id or 'new'}.json"
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "operation": operation,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "before": before,
        "params": params,
        "after": after,
    }
    (BACKUPS_DIR / fname).write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    return fname


async def _fetch_current(entity_type: str, entity_id: int) -> dict | None:
    try:
        if entity_type == "deal":
            return (await _request("GET", f"/deals/{entity_id}")).get("data")
        if entity_type == "person":
            return (await _request("GET", f"/persons/{entity_id}")).get("data")
        if entity_type == "organization":
            return (await _request("GET", f"/organizations/{entity_id}")).get("data")
        if entity_type == "activity":
            return (await _request("GET", f"/activities/{entity_id}")).get("data")
        if entity_type == "deal_field":
            return (await _request("GET", f"/dealFields/{entity_id}", v1=True)).get("data")
    except httpx.HTTPStatusError:
        return None
    return None


@mcp.tool()
async def list_backups(limit: int = 30) -> list[dict]:
    """List recent backup snapshots (most recent first). Use restore_backup with the filename to undo."""
    files = sorted(BACKUPS_DIR.glob("*.json"), reverse=True)[:limit]
    out = []
    for f in files:
        d = json.loads(f.read_text())
        out.append({
            "file": f.name,
            "timestamp": d["timestamp"],
            "operation": d["operation"],
            "entity_type": d["entity_type"],
            "entity_id": d["entity_id"],
        })
    return out


@mcp.tool()
async def restore_backup(backup_file: str) -> dict:
    """Restore the state captured in a backup file (undo a previous operation).

    - CREATE ops → deletes the entity that was created.
    - UPDATE ops → PATCHes the entity back to the `before` state.
    - DELETE ops → re-creates the entity from `before` (note: for remove_deal_field,
      only the schema is recreated; per-deal values are permanently lost).
    """
    path = BACKUPS_DIR / backup_file
    if not path.exists():
        return {"error": f"backup not found: {backup_file}"}
    snap = json.loads(path.read_text())
    et = snap["entity_type"]
    eid = snap["entity_id"]
    before = snap.get("before")
    after = snap.get("after")

    if before is None and after is not None:
        return await _restore_delete_created(et, eid)
    if before is not None and after is None:
        return await _restore_recreate(et, before)
    if before is not None and after is not None:
        return await _restore_update(et, eid, before)
    return {"error": "unable to determine restore strategy"}


async def _restore_delete_created(entity_type: str, entity_id: int) -> dict:
    if entity_type == "deal_field":
        return await _request("DELETE", f"/dealFields/{entity_id}", v1=True)
    if entity_type == "note":
        return await _request("DELETE", f"/notes/{entity_id}", v1=True)
    paths = {"deal": "/deals", "person": "/persons", "organization": "/organizations", "activity": "/activities"}
    if entity_type in paths:
        return await _request("DELETE", f"{paths[entity_type]}/{entity_id}")
    return {"error": f"unsupported entity_type: {entity_type}"}


async def _restore_recreate(entity_type: str, before: dict) -> dict:
    if entity_type == "deal_field":
        body = {"name": before["name"], "field_type": before["field_type"]}
        if before.get("options"):
            body["options"] = before["options"]
        return await _request("POST", "/dealFields", v1=True, json=body)
    return {"error": f"recreate not supported for {entity_type} — data may be lost"}


async def _restore_update(entity_type: str, entity_id: int, before: dict) -> dict:
    READ_ONLY = {"id", "add_time", "update_time", "creator_user_id", "stage_change_time", "won_time", "lost_time", "close_time"}
    payload = {k: v for k, v in before.items() if k not in READ_ONLY and v is not None}
    paths = {"deal": "/deals", "person": "/persons", "organization": "/organizations", "activity": "/activities"}
    if entity_type in paths:
        return await _request("PATCH", f"{paths[entity_type]}/{entity_id}", json=payload)
    if entity_type == "note":
        return await _request("PUT", f"/notes/{entity_id}", v1=True, json={"content": before.get("content", "")})
    return {"error": f"update restore not supported for {entity_type}"}


# ===== Deals =====

@mcp.tool()
async def list_deals(
    pipeline_id: int = DEFAULT_PIPELINE_ID,
    status: Literal["open", "won", "lost", "deleted", "all_not_deleted"] = "open",
    limit: int = 50,
) -> dict:
    """List deals. Defaults to Renda Fixa pipeline (id=6) + open status.

    Pass pipeline_id=None to query across ALL pipelines.
    """
    params: dict[str, Any] = {"status": status, "limit": limit}
    if pipeline_id:
        params["pipeline_id"] = pipeline_id
    return await _request("GET", "/deals", params=params)


@mcp.tool()
async def get_deal(deal_id: int) -> dict:
    """Fetch full details of a single deal."""
    return await _request("GET", f"/deals/{deal_id}")


@mcp.tool()
async def search_deals(term: str, limit: int = 20) -> dict:
    """Search deals by title or custom field content."""
    return await _request("GET", "/deals/search", params={"term": term, "limit": limit})


@mcp.tool()
async def update_deal(
    deal_id: int,
    value: float | None = None,
    currency: str | None = None,
    title: str | None = None,
    stage_id: int | None = None,
    pipeline_id: int | None = None,
    status: Literal["open", "won", "lost"] | None = None,
    person_id: int | None = None,
    org_id: int | None = None,
    expected_close_date: str | None = None,
    custom_fields: dict[str, Any] | None = None,
) -> dict:
    """Update one or more fields on a deal. Auto-snapshots before+after for undo.

    Essential fields (title, value, org_id, person_id, stage_id, etc.) cannot be
    blanked — passing "" for title is rejected. To change them, pass the new value.
    """
    if title is not None and not str(title).strip():
        return {"error": "title is an essential field and cannot be blank — pass a non-empty title"}
    if currency is not None and not str(currency).strip():
        return {"error": "currency is essential and cannot be blank"}

    if custom_fields:
        protected_in_cf = [k for k in custom_fields if k in PROTECTED_FIELD_KEYS]
        if protected_in_cf:
            return {"error": f"custom_fields contains protected keys {protected_in_cf} — use the dedicated parameters instead"}

    payload: dict[str, Any] = {}
    for k, v in [
        ("value", value), ("currency", currency), ("title", title),
        ("stage_id", stage_id), ("pipeline_id", pipeline_id), ("status", status),
        ("person_id", person_id), ("org_id", org_id),
        ("expected_close_date", expected_close_date),
    ]:
        if v is not None:
            payload[k] = v
    if custom_fields:
        payload["custom_fields"] = custom_fields

    before = await _fetch_current("deal", deal_id)
    result = await _request("PATCH", f"/deals/{deal_id}", json=payload)
    after = result.get("data")
    backup = _write_snapshot("update_deal", "deal", deal_id, before, payload, after)
    return {"result": result, "_backup": backup}


@mcp.tool()
async def move_deal_to_stage(deal_id: int, stage_id: int) -> dict:
    """Move a deal to a specific stage. Use list_stages to find stage_id. Auto-backed up."""
    return await update_deal(deal_id=deal_id, stage_id=stage_id)


@mcp.tool()
async def clear_deal_custom_field(deal_id: int, field_key: str) -> dict:
    """Clear (set to null) one custom field on a deal. Auto-backed up.

    Refuses to clear essential fields (value, title, org_id, etc.) — those define
    the deal and shouldn't be blanked. Change them via update_deal with a new value.
    """
    if field_key in PROTECTED_FIELD_KEYS:
        return {"error": f"field {field_key!r} is essential and cannot be cleared. Use update_deal to change it to a new value."}
    return await update_deal(deal_id=deal_id, custom_fields={field_key: None})


@mcp.tool()
async def add_note_to_deal(deal_id: int, content: str) -> dict:
    """Attach a note (descrição rica, aceita HTML) to a deal. Notes API is v1. Auto-backed up."""
    result = await _request("POST", "/notes", v1=True, json={"deal_id": deal_id, "content": content})
    note_id = (result.get("data") or {}).get("id")
    backup = _write_snapshot("add_note", "note", note_id, None, {"deal_id": deal_id, "content": content}, result.get("data"))
    return {"result": result, "_backup": backup}


# ===== Activities =====

@mcp.tool()
async def link_meeting_to_deal(
    deal_id: int,
    subject: str,
    due_date: str,
    due_time: str | None = None,
    duration: str | None = None,
    note: str | None = None,
    participants: list[int] | None = None,
) -> dict:
    """Create a meeting activity linked to a deal.

    - due_date: "YYYY-MM-DD"; due_time: "HH:MM"; duration: "HH:MM"
    - participants: list of person_ids attending
    """
    payload: dict[str, Any] = {
        "deal_id": deal_id, "subject": subject, "type": "meeting",
        "due_date": due_date,
    }
    if due_time:
        payload["due_time"] = due_time
    if duration:
        payload["duration"] = duration
    if note:
        payload["note"] = note
    if participants:
        payload["participants"] = [{"person_id": p, "primary": False} for p in participants]
    result = await _request("POST", "/activities", json=payload)
    aid = (result.get("data") or {}).get("id")
    backup = _write_snapshot("link_meeting", "activity", aid, None, payload, result.get("data"))
    return {"result": result, "_backup": backup}


@mcp.tool()
async def create_follow_up(
    deal_id: int,
    subject: str,
    due_date: str,
    activity_type: Literal["task", "call", "email", "meeting"] = "task",
    note: str | None = None,
) -> dict:
    """Create a follow-up (task/call/email) linked to a deal. Auto-backed up."""
    payload: dict[str, Any] = {
        "deal_id": deal_id, "subject": subject, "type": activity_type,
        "due_date": due_date,
    }
    if note:
        payload["note"] = note
    result = await _request("POST", "/activities", json=payload)
    aid = (result.get("data") or {}).get("id")
    backup = _write_snapshot("create_follow_up", "activity", aid, None, payload, result.get("data"))
    return {"result": result, "_backup": backup}


@mcp.tool()
async def list_activities_for_deal(deal_id: int, done: bool | None = None) -> dict:
    """List activities on a deal. done=False=pending only, True=completed only, None=both."""
    params: dict[str, Any] = {"deal_id": deal_id, "limit": 100}
    if done is not None:
        params["done"] = "true" if done else "false"
    return await _request("GET", "/activities", params=params)


@mcp.tool()
async def mark_activity_done(activity_id: int, done: bool = True) -> dict:
    """Mark an activity done (or reopen it). Auto-backed up."""
    before = await _fetch_current("activity", activity_id)
    result = await _request("PATCH", f"/activities/{activity_id}", json={"done": done})
    after = result.get("data")
    backup = _write_snapshot("mark_activity_done", "activity", activity_id, before, {"done": done}, after)
    return {"result": result, "_backup": backup}


# ===== Persons (contacts) =====

@mcp.tool()
async def search_persons(term: str, limit: int = 20) -> dict:
    """Search persons by name, email, or phone."""
    return await _request(
        "GET", "/persons/search",
        params={"term": term, "limit": limit, "fields": "name,email,phone"},
    )


@mcp.tool()
async def get_person(person_id: int) -> dict:
    """Fetch a person's full record."""
    return await _request("GET", f"/persons/{person_id}")


@mcp.tool()
async def create_person(
    name: str,
    email: str | None = None,
    phone: str | None = None,
    org_id: int | None = None,
) -> dict:
    """Create a new person. Email/phone added as primary 'work' contact. Auto-backed up."""
    payload: dict[str, Any] = {"name": name}
    if email:
        payload["emails"] = [{"value": email, "primary": True, "label": "work"}]
    if phone:
        payload["phones"] = [{"value": phone, "primary": True, "label": "work"}]
    if org_id:
        payload["org_id"] = org_id
    result = await _request("POST", "/persons", json=payload)
    pid = (result.get("data") or {}).get("id")
    backup = _write_snapshot("create_person", "person", pid, None, payload, result.get("data"))
    return {"result": result, "_backup": backup}


@mcp.tool()
async def update_person(
    person_id: int,
    name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    org_id: int | None = None,
) -> dict:
    """Update a person. Email/phone REPLACE the primary entry. Auto-backed up."""
    before = await _fetch_current("person", person_id)
    payload: dict[str, Any] = {}
    if name is not None:
        payload["name"] = name
    if email is not None:
        payload["emails"] = [{"value": email, "primary": True, "label": "work"}]
    if phone is not None:
        payload["phones"] = [{"value": phone, "primary": True, "label": "work"}]
    if org_id is not None:
        payload["org_id"] = org_id
    result = await _request("PATCH", f"/persons/{person_id}", json=payload)
    after = result.get("data")
    backup = _write_snapshot("update_person", "person", person_id, before, payload, after)
    return {"result": result, "_backup": backup}


@mcp.tool()
async def link_person_to_deal(deal_id: int, person_id: int) -> dict:
    """Set person_id on a deal (replaces any previously-linked contact). Auto-backed up."""
    return await update_deal(deal_id=deal_id, person_id=person_id)


# ===== Organizations =====

@mcp.tool()
async def search_organizations(term: str, limit: int = 20) -> dict:
    """Search organizations by name."""
    return await _request(
        "GET", "/organizations/search",
        params={"term": term, "limit": limit, "fields": "name"},
    )


@mcp.tool()
async def get_organization(org_id: int) -> dict:
    """Fetch an organization's full record."""
    return await _request("GET", f"/organizations/{org_id}")


@mcp.tool()
async def create_organization(name: str, address: str | None = None) -> dict:
    """Create a new organization. Auto-backed up."""
    payload: dict[str, Any] = {"name": name}
    if address:
        payload["address"] = address
    result = await _request("POST", "/organizations", json=payload)
    oid = (result.get("data") or {}).get("id")
    backup = _write_snapshot("create_organization", "organization", oid, None, payload, result.get("data"))
    return {"result": result, "_backup": backup}


@mcp.tool()
async def link_organization_to_deal(deal_id: int, org_id: int) -> dict:
    """Set org_id on a deal. Auto-backed up."""
    return await update_deal(deal_id=deal_id, org_id=org_id)


# ===== Combined convenience =====

@mcp.tool()
async def attach_contact_to_deal(
    deal_id: int,
    contact_email: str,
    contact_name: str | None = None,
    company_name: str | None = None,
) -> dict:
    """Find-or-create a person (by email) and optionally an organization (by name),
    then link both to the deal.

    Flow:
      1. Search person by email — if not found, create (requires contact_name).
      2. If company_name given, search org — if not found, create.
      3. Link person+org to the deal.

    Each sub-operation is independently backed up. To fully undo, restore each
    backup file in reverse order (use list_backups).
    """
    search = await _request("GET", "/persons/search", params={"term": contact_email, "fields": "email"})
    items = (search.get("data") or {}).get("items") or []
    if items:
        person_id = items[0]["item"]["id"]
        person_action = "found_existing"
    else:
        if not contact_name:
            return {"error": f"person not found by email {contact_email!r} — provide contact_name to create"}
        created = await create_person(name=contact_name, email=contact_email)
        person_id = ((created.get("result") or {}).get("data") or {}).get("id")
        person_action = "created"

    org_id = None
    org_action = None
    if company_name:
        search_org = await _request("GET", "/organizations/search", params={"term": company_name, "fields": "name"})
        org_items = (search_org.get("data") or {}).get("items") or []
        if org_items:
            org_id = org_items[0]["item"]["id"]
            org_action = "found_existing"
        else:
            created_org = await create_organization(name=company_name)
            org_id = ((created_org.get("result") or {}).get("data") or {}).get("id")
            org_action = "created"
        if person_action == "created":
            await update_person(person_id=person_id, org_id=org_id)

    link_kwargs: dict[str, Any] = {"person_id": person_id}
    if org_id:
        link_kwargs["org_id"] = org_id
    link_result = await update_deal(deal_id=deal_id, **link_kwargs)

    return {
        "deal_id": deal_id,
        "person_id": person_id, "person_action": person_action,
        "org_id": org_id, "org_action": org_action,
        "deal_update": link_result,
    }


# ===== Deal Fields (schema management) =====

@mcp.tool()
async def list_deal_fields() -> dict:
    """List all deal fields (default + custom) with their `key` (used in custom_fields dict) and `field_type`.

    Use the returned `key` in update_deal(custom_fields={key: value}).
    """
    result = await _request("GET", "/dealFields", v1=True, params={"limit": 200})
    fields = [
        {
            "id": f["id"],
            "key": f["key"],
            "name": f["name"],
            "field_type": f["field_type"],
            "is_custom": bool(f.get("edit_flag")),
            "options": f.get("options"),
        }
        for f in (result.get("data") or [])
    ]
    return {"fields": fields, "count": len(fields)}


VALID_FIELD_TYPES = [
    "varchar", "varchar_auto", "text", "double", "monetary", "date", "set",
    "enum", "user", "org", "people", "phone", "time", "timerange", "daterange", "address",
]


@mcp.tool()
async def add_deal_field(
    name: str,
    field_type: str,
    options: list[str] | None = None,
) -> dict:
    """Create a new custom deal field on the Pipedrive account.

    - field_type: varchar | text | double | monetary | date | enum | set | address | phone | time | etc.
    - options: list of labels for enum/set (dropdown) fields.

    Returns the new field's id and `key` to use in update_deal(custom_fields={key: value}).
    Auto-backed up.
    """
    if field_type not in VALID_FIELD_TYPES:
        return {"error": f"invalid field_type {field_type!r}. Valid: {VALID_FIELD_TYPES}"}
    payload: dict[str, Any] = {"name": name, "field_type": field_type}
    if options:
        payload["options"] = [{"label": o} for o in options]
    result = await _request("POST", "/dealFields", v1=True, json=payload)
    data = result.get("data") or {}
    fid = data.get("id")
    backup = _write_snapshot("add_deal_field", "deal_field", fid, None, payload, data)
    return {"result": result, "field_key": data.get("key"), "field_id": fid, "_backup": backup}


@mcp.tool()
async def remove_deal_field(field_id: int) -> dict:
    """DELETE a custom deal field. WARNING: per-deal values across ALL deals are PERMANENTLY lost.

    Refuses to delete:
      - Essential keys (value, title, org_id, person_id, stage_id, etc.) — listed in PROTECTED_FIELD_KEYS
      - System fields (edit_flag=false) — Pipedrive API also blocks these

    Backup recreates the schema on restore, but per-deal values are unrecoverable.
    Prefer clear_deal_custom_field for per-deal cleanups.
    """
    before = await _fetch_current("deal_field", field_id)
    if not before:
        return {"error": f"deal field id={field_id} not found"}

    key = before.get("key")
    name = before.get("name")
    if key in PROTECTED_FIELD_KEYS:
        return {"error": f"field {name!r} (key={key}) is essential — removal blocked. Protected keys: {sorted(PROTECTED_FIELD_KEYS)}"}
    if not before.get("edit_flag"):
        return {"error": f"field {name!r} is a built-in system field (edit_flag=false) — Pipedrive does not allow deletion"}

    result = await _request("DELETE", f"/dealFields/{field_id}", v1=True)
    backup = _write_snapshot("remove_deal_field", "deal_field", field_id, before, None, None)
    return {
        "result": result,
        "_backup": backup,
        "warning": "field values across all deals are permanently lost — restore only recreates the schema",
    }


# ===== Pipeline metadata =====

@mcp.tool()
async def list_pipelines() -> dict:
    """List all pipelines with ids and names."""
    return await _request("GET", "/pipelines")


@mcp.tool()
async def list_stages(pipeline_id: int = DEFAULT_PIPELINE_ID) -> dict:
    """List stages of a pipeline (default: Renda Fixa, id=6). Returns stage_ids for move_deal_to_stage."""
    return await _request("GET", "/stages", params={"pipeline_id": pipeline_id})


def _build_http_app(auth_token: str):
    """Wrap the FastMCP streamable-http app with bearer-token auth + health route."""
    import hmac
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse, PlainTextResponse

    class BearerAuth(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            path = request.url.path
            if path == "/health":
                return PlainTextResponse("ok")
            if path == "/":
                return PlainTextResponse(
                    "pipedrive-yvy MCP — POST /mcp com header Authorization: Bearer <token>"
                )
            header = request.headers.get("Authorization", "")
            if not header.startswith("Bearer "):
                return JSONResponse({"error": "missing bearer token"}, status_code=401)
            if not hmac.compare_digest(header[7:], auth_token):
                return JSONResponse({"error": "invalid bearer token"}, status_code=401)
            return await call_next(request)

    app = mcp.streamable_http_app()
    app.add_middleware(BearerAuth)
    return app


def main() -> None:
    transport = os.environ.get("MCP_TRANSPORT", "stdio").lower()
    if transport == "stdio":
        mcp.run()
        return

    if transport in ("http", "streamable-http"):
        auth_token = os.environ.get("MCP_AUTH_TOKEN")
        if not auth_token:
            raise SystemExit("MCP_AUTH_TOKEN is required when MCP_TRANSPORT=http")
        import uvicorn
        port = int(os.environ.get("PORT", "8000"))
        app = _build_http_app(auth_token)
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
        return

    raise SystemExit(f"unknown MCP_TRANSPORT={transport!r} — use 'stdio' or 'http'")


if __name__ == "__main__":
    main()
