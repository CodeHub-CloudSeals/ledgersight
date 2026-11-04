"""LedgerSight reconciliation workspace (customer-operable).

Run:  uvicorn ledgersight.app:app --port 8090 --workers 1
One process, one worker: state and the evidence chains are in memory.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from .auth import REQUIRE_MFA, AuthStore, otpauth_uri
from .core.ingest import IngestError, MAX_BYTES
from .service import FIXTURES, WORKSPACES, Denied, Service

BASE = Path(__file__).resolve().parent
COOKIE = "ls_session"
SECURE_COOKIE = os.environ.get("AVOCET_SECURE_COOKIE", "1") == "1"

app = FastAPI(title="LedgerSight", version="0.5.0",
              docs_url=None, redoc_url=None, openapi_url=None)
auth = AuthStore()
svc = Service()
_boot = auth.bootstrap(set(WORKSPACES))
if _boot:
    print(f"[ledgersight] one-time presenter invite: /invite?token={_boot.token}", file=sys.stderr, flush=True)

app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")


_G = bool(os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_API_KEY"))
CSP = ("default-src 'self'; img-src 'self' data:" + (" https://*.googleusercontent.com https://ssl.gstatic.com" if _G else "") +
       "; style-src 'self'" + (" 'unsafe-inline' https://fonts.googleapis.com" if _G else "") +
       "; script-src 'self'" + (" https://apis.google.com https://accounts.google.com" if _G else "") +
       ("; frame-src https://docs.google.com https://accounts.google.com https://content.googleapis.com" if _G else "") +
       ("; connect-src 'self' https://www.googleapis.com https://content.googleapis.com" if _G else "") +
       "; frame-ancestors 'none'")


@app.middleware("http")
async def headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.update({
        "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
        "Cache-Control": "no-store",
        "Content-Security-Policy": CSP,
    })
    if SECURE_COOKIE:
        resp.headers["Strict-Transport-Security"] = "max-age=31536000"
    return resp


def current_user(request: Request):
    u = auth.resolve(request.cookies.get(COOKIE))
    if not u:
        raise HTTPException(401, "sign in required")
    # CSRF: mutating calls must carry the custom header (cross-site forms cannot set it).
    if request.method in ("POST", "PUT", "DELETE") and request.headers.get("x-ledgersight") != "1":
        raise HTTPException(403, "missing request header")
    return u


def handle(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except Denied as d:
        if d.conceal:
            raise HTTPException(404, "not found")
        raise HTTPException(403, {"decision": "DENY", "reason_code": d.code})
    except (IngestError, ValueError) as e:
        raise HTTPException(422, str(e))
    except KeyError:
        raise HTTPException(404, "not found")


# ---- pages ---------------------------------------------------------------
@app.get("/")
def index():
    return FileResponse(BASE / "static" / "index.html")


@app.get("/invite")
def invite_page():
    return FileResponse(BASE / "static" / "invite.html")


@app.get("/v1/auth-config")
def auth_config():
    return {"mfa": REQUIRE_MFA}


@app.get("/healthz")
def healthz():
    return {"status": "ok", "service": "ledgersight"}


# ---- identity ------------------------------------------------------------
@app.get("/v1/invites/{token}")
def invite_info(token: str):
    try:
        inv = auth.get_invite(token)
    except PermissionError as e:
        raise HTTPException(410, str(e))
    u = auth.users[inv.user_id]
    return {"user_id": u.user_id, "display_name": u.display_name, "role": u.role,
            "account_expires_at": u.expires_at.isoformat(), "totp_secret": inv.totp,
            "otpauth_uri": otpauth_uri(inv.totp, u.user_id)}


@app.post("/v1/invites/{token}/accept")
def invite_accept(token: str, body: dict = Body(...)):
    try:
        u = auth.accept(token, body.get("password", ""), body.get("code", ""), bool(body.get("terms")))
    except PermissionError as e:
        raise HTTPException(410, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"ok": True, "user_id": u.user_id}


@app.post("/v1/login")
def login(body: dict = Body(...)):
    try:
        tok = auth.login(body.get("user_id", ""), body.get("password", ""), body.get("code", ""))
    except PermissionError as e:
        raise HTTPException(401, str(e))
    resp = JSONResponse({"ok": True})
    resp.set_cookie(COOKIE, tok, httponly=True, secure=SECURE_COOKIE, samesite="strict", max_age=8 * 3600)
    return resp


@app.post("/v1/logout")
def logout(request: Request):
    auth.logout(request.cookies.get(COOKIE, ""))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE)
    return resp


@app.get("/v1/me")
def me(u=Depends(current_user)):
    return {**u.public(), "support_contact": os.environ.get("AVOCET_SUPPORT_CONTACT", "support@cloudseals.com")}


@app.get("/v1/users")
def users(u=Depends(current_user)):
    if u.role != "owner_reviewer":
        raise HTTPException(403, {"decision": "DENY", "reason_code": "ROLE_NOT_AUTHORISED"})
    return [x.public() for x in auth.users.values() if x.workspaces & u.workspaces or x.role == "support"]


@app.post("/v1/invites")
def create_invite(request: Request, body: dict = Body(...), u=Depends(current_user)):
    if u.role != "owner_reviewer":
        raise HTTPException(403, {"decision": "DENY", "reason_code": "ROLE_NOT_AUTHORISED"})
    ws = set(body.get("workspaces") or [])
    if not ws <= u.workspaces:
        raise HTTPException(403, {"decision": "DENY", "reason_code": "WORKSPACE_SCOPE"})
    try:
        inv = auth.invite(body["user_id"], body.get("display_name", body["user_id"]), body.get("role", "preparer"),
                          ws, int(body.get("account_days", 14)))
    except (KeyError, ValueError) as e:
        raise HTTPException(422, str(e))
    return {"invite_url": f"{request.base_url}invite?token={inv.token}", "expires_at": inv.expires_at.isoformat()}


@app.post("/v1/users/{user_id}/revoke")
def revoke(user_id: str, u=Depends(current_user)):
    if u.role != "owner_reviewer" or user_id not in auth.users or user_id == u.user_id:
        raise HTTPException(403, {"decision": "DENY", "reason_code": "ROLE_NOT_AUTHORISED"})
    auth.revoke(user_id)
    return {"revoked": user_id}


# ---- workspaces ------------------------------------------------------------
@app.get("/v1/workspaces")
def workspaces(u=Depends(current_user)):
    return svc.workspaces_for(u)


@app.get("/v1/workspaces/{wid}")
def workspace(wid: str, u=Depends(current_user)):
    w = handle(svc.gate, u, wid, "workspace.read")
    return {"id": wid, **w.meta, "status": svc.status(wid), "uploads": list(w.uploads.values()),
            "documents": svc.documents(w),
            "runs": [{"id": r["id"], "created_at": r["created_at"], "created_by": r["created_by"]} for r in w.runs.values()]}


@app.post("/v1/workspaces/{wid}/uploads")
async def upload(wid: str, kind: str = Form(...), data_class: str = Form("synthetic"),
                 file: UploadFile = File(...), u=Depends(current_user)):
    blob = await file.read(MAX_BYTES + 1)
    return handle(svc.upload, u, wid, kind, file.filename or "upload", blob, data_class)


@app.post("/v1/workspaces/{wid}/email")
async def read_email(wid: str, file: UploadFile = File(...), u=Depends(current_user)):
    blob = await file.read(25 * 1024 * 1024 + 1)
    return handle(svc.read_email, u, wid, blob)


@app.get("/v1/integrations")
def integrations(u=Depends(current_user)):
    """Which outside sources are switched on for this deployment. Keys come from the environment."""
    gid, gkey = os.environ.get("GOOGLE_CLIENT_ID"), os.environ.get("GOOGLE_API_KEY")
    return {"google_drive": {"enabled": bool(gid and gkey), "client_id": gid, "api_key": gkey,
                             "app_id": os.environ.get("GOOGLE_APP_ID", (gid or "").split("-")[0])}}


@app.post("/v1/workspaces/{wid}/documents")
async def upload_pdf(wid: str, data_class: str = Form("synthetic"), file: UploadFile = File(...), u=Depends(current_user)):
    blob = await file.read(MAX_BYTES + 1)
    return handle(svc.upload_pdf, u, wid, file.filename or "document.pdf", blob, data_class)


@app.post("/v1/workspaces/{wid}/documents/{doc_id}/confirm")
def confirm_pdf(wid: str, doc_id: str, body: dict = Body(...), u=Depends(current_user)):
    return handle(svc.confirm_pdf, u, wid, doc_id, body.get("fields") or {}, body.get("doc_type"))


@app.post("/v1/workspaces/{wid}/uploads/{upload_id}/mapping")
def commit_mapping(wid: str, upload_id: str, body: dict = Body(...), u=Depends(current_user)):
    return handle(svc.commit_mapping, u, wid, upload_id, body.get("mapping") or {})


@app.post("/v1/workspaces/{wid}/samples/load")
def load_sample(wid: str, u=Depends(current_user)):
    return handle(svc.load_sample, u, wid)


@app.get("/v1/workspaces/{wid}/samples/{name}")
def sample_file(wid: str, name: str, u=Depends(current_user)):
    handle(svc.gate, u, wid, "workspace.read")
    if not name.startswith(wid + "_") or "/" in name or name.endswith(".json") or not (FIXTURES / name).is_file():
        raise HTTPException(404, "not found")
    return FileResponse(FIXTURES / name, filename=name)


@app.post("/v1/workspaces/{wid}/reconciliation-runs")
def create_run(wid: str, u=Depends(current_user)):
    r = handle(svc.run, u, wid)
    return {k: v for k, v in r.items() if k != "batch_key"}


@app.get("/v1/workspaces/{wid}/runs/{run_id}")
def get_run(wid: str, run_id: str, u=Depends(current_user)):
    w = handle(svc.gate, u, wid, "workspace.read", run_id=run_id)
    if run_id not in w.runs:
        raise HTTPException(404, "not found")
    return {k: v for k, v in w.runs[run_id].items() if k != "batch_key"}


@app.post("/v1/workspaces/{wid}/runs/{run_id}/exceptions/{exc_id}/decisions")
def decide(wid: str, run_id: str, exc_id: str, body: dict = Body(...), u=Depends(current_user)):
    return handle(svc.decide, u, wid, run_id, exc_id, body.get("decision"), body.get("note", ""))


@app.post("/v1/workspaces/{wid}/runs/{run_id}/exceptions/{exc_id}/proposals")
def propose(wid: str, run_id: str, exc_id: str, body: dict = Body(...), u=Depends(current_user)):
    return handle(svc.propose, u, wid, run_id, exc_id, body.get("note", ""))


@app.post("/v1/workspaces/{wid}/actions/attempt")
def attempt(wid: str, body: dict = Body(...), u=Depends(current_user)):
    """Security demonstration: ask the gate for an action. Never executes anything."""
    action = body.get("action", "")
    try:
        svc.gate(u, wid, action, data_class=body.get("data_class", "synthetic"),
                 model_route=body.get("model_route", "deterministic"), maker_id=body.get("maker_id"))
    except Denied as d:
        return JSONResponse(status_code=404 if d.conceal else 403,
                            content={"decision": "DENY", "reason_code": d.code, "executed": False})
    return {"decision": "ALLOW", "executed": False, "note": "Gate allowed the request; this endpoint never executes."}


@app.get("/v1/workspaces/{wid}/parties")
def parties(wid: str, u=Depends(current_user)):
    return handle(svc.parties, u, wid)


@app.get("/v1/workspaces/{wid}/parties/{pid}")
def party(wid: str, pid: str, u=Depends(current_user)):
    return handle(svc.party, u, wid, pid)


@app.post("/v1/workspaces/{wid}/parties/{pid}/links/{line_id}")
def party_link(wid: str, pid: str, line_id: str, body: dict = Body(...), u=Depends(current_user)):
    return handle(svc.review_party_link, u, wid, line_id, pid, body.get("decision"), body.get("note", ""))


@app.get("/v1/workspaces/{wid}/trace")
def trace_index(wid: str, u=Depends(current_user)):
    return handle(svc.trace_index, u, wid)


@app.get("/v1/workspaces/{wid}/trace/{line_id}")
def trace(wid: str, line_id: str, u=Depends(current_user)):
    return handle(svc.trace, u, wid, line_id)


@app.get("/v1/workspaces/{wid}/evidence")
def evidence(wid: str, u=Depends(current_user)):
    return handle(svc.evidence, u, wid)


@app.post("/v1/workspaces/{wid}/evidence/tamper-check")
def tamper_check(wid: str, u=Depends(current_user)):
    return handle(svc.tamper_check, u, wid)


@app.get("/v1/workspaces/{wid}/runs/{run_id}/export.csv")
def export_csv(wid: str, run_id: str, u=Depends(current_user)):
    data = handle(svc.export_csv, u, wid, run_id)
    return PlainTextResponse(data, media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{wid}_{run_id}_review.csv"'})


@app.get("/v1/workspaces/{wid}/runs/{run_id}/review-pack.json")
def export_pack(wid: str, run_id: str, u=Depends(current_user)):
    data = handle(svc.review_pack, u, wid, run_id)
    return JSONResponse(data, headers={"Content-Disposition": f'attachment; filename="{wid}_{run_id}_review_pack.json"'})


@app.post("/v1/workspaces/{wid}/sources/{kind}/clear")
def clear_source(wid: str, kind: str, u=Depends(current_user)):
    return handle(svc.clear_source, u, wid, kind)


@app.post("/v1/workspaces/{wid}/reset")
def reset(wid: str, u=Depends(current_user)):
    return handle(svc.reset, u, wid)


@app.get("/v1/trust")
def trust(u=Depends(current_user)):
    return svc.trust()
