"""LedgerSight reconciliation workspace (customer-operable).

Run:  uvicorn ledgersight.app:app --port 8090 --workers 1
One process, one worker: state and the evidence chains are in memory.
"""
from __future__ import annotations

import os
import re
import sys
import uuid
from pathlib import Path

from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token

from datetime import timedelta

from datetime import datetime

from .auth import REQUIRE_MFA, AuthStore, Invite, User, now, otpauth_uri
from .core import ingest
from .core.ingest import IngestError, MAX_BYTES
from .core.reports import FORMATS as REPORT_TYPES
from .service import FIXTURES, WORKSPACES, Denied, Service

BASE = Path(__file__).resolve().parent
COOKIE = "ls_session"
SECURE_COOKIE = os.environ.get("AVOCET_SECURE_COOKIE", "1") == "1"

APP_VERSION = "1.0.0"
app = FastAPI(title="LedgerSight", version=APP_VERSION,
              docs_url=None, redoc_url=None, openapi_url=None)
auth = AuthStore()
svc = Service()


# ---- user directory survives restarts (sessions do not: people sign in again) ----
def _dt(v):
    return datetime.fromisoformat(v) if v else None


def load_users():
    if not svc.store:
        return
    for d in svc.store.get("users", []):
        auth.users[d["user_id"]] = User(d["user_id"], d["display_name"], d["role"], set(d["workspaces"]), d.get("pw_hash"),
                                        d.get("totp"), d.get("active", False), _dt(d["expires_at"]), d.get("revoked", False),
                                        d.get("failed", 0), _dt(d.get("terms_accepted_at")))
    for d in svc.store.get("invites", []):
        auth.invites[d["token"]] = Invite(d["token"], d["user_id"], _dt(d["expires_at"]), d["totp"], d.get("used", False))


def save_users():
    if not svc.store:
        return
    svc.store.put("users", [{**{k: getattr(u, k) for k in ("user_id", "display_name", "role", "pw_hash", "totp", "active",
                                                            "revoked", "failed")},
                             "workspaces": sorted(u.workspaces), "expires_at": u.expires_at.isoformat(),
                             "terms_accepted_at": u.terms_accepted_at.isoformat() if u.terms_accepted_at else None}
                            for u in auth.users.values()])
    svc.store.put("invites", [{"token": i.token, "user_id": i.user_id, "expires_at": i.expires_at.isoformat(), "totp": i.totp,
                               "used": i.used} for i in auth.invites.values()])


load_users()
_boot = auth.bootstrap(set(WORKSPACES))
if _boot:
    print(f"[ledgersight] one-time presenter invite: /invite?token={_boot.token}", file=sys.stderr, flush=True)

app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")


_G = bool(os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_API_KEY"))
GOOGLE_LOGIN_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_LOGIN_DOMAIN = os.environ.get("AVOCET_GOOGLE_LOGIN_DOMAIN")
_GL = bool(GOOGLE_LOGIN_CLIENT_ID and GOOGLE_LOGIN_DOMAIN)
_GOOGLE_WEB = _G or _GL
CSP = ("default-src 'self'; img-src 'self' data:" + (" https://*.googleusercontent.com https://ssl.gstatic.com" if _GOOGLE_WEB else "") +
       "; style-src 'self'" + (" 'unsafe-inline' https://fonts.googleapis.com https://accounts.google.com" if _GOOGLE_WEB else "") +
       "; script-src 'self'" + (" https://apis.google.com https://accounts.google.com" if _GOOGLE_WEB else "") +
       ("; frame-src https://docs.google.com https://accounts.google.com https://content.googleapis.com" if _GOOGLE_WEB else "") +
       ("; connect-src 'self' https://www.googleapis.com https://content.googleapis.com" if _GOOGLE_WEB else "") +
       "; frame-ancestors 'none'")


_WS_PATH = re.compile(r"^/v1/workspaces/([^/]+)")


@app.middleware("http")
async def headers(request: Request, call_next):
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
    resp = await call_next(request)
    # Persist after every request: reads add evidence events too, so the audit trail is always on disk.
    try:
        m = _WS_PATH.match(request.url.path)
        svc.save(m.group(1) if m else None)
        if request.method != "GET" and request.url.path.startswith(("/v1/invites", "/v1/users", "/v1/login")):
            save_users()
    except Exception as e:  # storage trouble is reported by /readyz and in the server log
        print(f"[ledgersight] storage error on {request.url.path} (request {rid}): {e!r}", file=sys.stderr, flush=True)
    resp.headers["X-Request-ID"] = rid
    resp.headers.update({
        "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
        "Cache-Control": "no-store",
        "Content-Security-Policy": CSP,
    })
    if SECURE_COOKIE:
        resp.headers["Strict-Transport-Security"] = "max-age=31536000"
    return resp


# ---- local demo mode: no login screen ------------------------------------------
# AVOCET_NO_LOGIN=1 skips sign-in for a presenter's laptop. It is honoured only when the local-demo
# settings are also on (MFA off, insecure cookie), so a hosted deployment cannot be opened by accident.
# The presenter switches between two demo people so maker-checker can still be shown.
NO_LOGIN = os.environ.get("AVOCET_NO_LOGIN", "0") == "1" and not REQUIRE_MFA and not SECURE_COOKIE
DEMO_COOKIE = "ls_demo_as"
DEMO_PEOPLE = {"reviewer": ("cpa-reviewer", "CPA Reviewer", "owner_reviewer"),
               "preparer": ("staff-preparer", "Staff Preparer", "preparer")}
if NO_LOGIN:
    print("[ledgersight] LOCAL DEMO MODE: login is switched off. Never use this setting on a shared or hosted server.",
          file=sys.stderr, flush=True)


def demo_user(key: str):
    uid, name, role = DEMO_PEOPLE.get(key, DEMO_PEOPLE["reviewer"])
    if uid not in auth.users:
        auth.users[uid] = User(uid, name, role, set(WORKSPACES), active=True,
                               expires_at=now() + timedelta(days=3650), terms_accepted_at=now())
    return auth.users[uid]


def current_user(request: Request):
    u = demo_user(request.cookies.get(DEMO_COOKIE, "reviewer")) if NO_LOGIN else auth.resolve(request.cookies.get(COOKIE))
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
        raise HTTPException(403, {"decision": "DENY", "reason_code": d.code, "policy_id": d.policy_id})
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
    return {"mfa": REQUIRE_MFA, "login_required": not NO_LOGIN, "version": APP_VERSION,
            "samples_enabled": os.environ.get("AVOCET_SHOW_SAMPLES", "0") == "1",
            "demo_people": [{"key": k, "name": v[1], "role": v[2]} for k, v in DEMO_PEOPLE.items()] if NO_LOGIN else [],
            "google_login": _GL, "google_client_id": GOOGLE_LOGIN_CLIENT_ID if _GL else None}


@app.post("/v1/demo/act-as")
def act_as(body: dict = Body(...), u=Depends(current_user)):
    """Local demo mode only: switch between the demo CPA reviewer and the demo staff preparer."""
    if not NO_LOGIN:
        raise HTTPException(404, "not found")
    key = body.get("as")
    if key not in DEMO_PEOPLE:
        raise HTTPException(422, "choose reviewer or preparer")
    resp = JSONResponse({"ok": True, **demo_user(key).public()})
    resp.set_cookie(DEMO_COOKIE, key, httponly=True, samesite="strict")
    return resp


@app.get("/healthz")
def healthz():
    return {"status": "ok", "service": "ledgersight", "version": APP_VERSION}


@app.get("/readyz")
def readyz():
    r = svc.ready()
    return JSONResponse(status_code=200 if r["ready"] else 503, content={**r, "version": APP_VERSION})


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


GOOGLE_SIGNIN_REFUSED = ("This account can't sign in to LedgerSight. Use your work Google account, "
                         "or ask your administrator for access.")


@app.post("/v1/login/google")
def login_google(body: dict = Body(...)):
    if not _GL:
        raise HTTPException(404, "not found")
    try:
        claims = google_id_token.verify_oauth2_token(body.get("credential", ""), google_requests.Request(), GOOGLE_LOGIN_CLIENT_ID)
    except Exception:
        raise HTTPException(401, "Google sign-in could not be verified")
    email = (claims.get("email") or "").lower()
    domain = claims.get("hd") or email.rsplit("@", 1)[-1]
    if not claims.get("email_verified") or domain != GOOGLE_LOGIN_DOMAIN or not email.endswith("@" + GOOGLE_LOGIN_DOMAIN):
        # Generic on purpose: never reveal which domain or accounts are allowed.
        raise HTTPException(403, GOOGLE_SIGNIN_REFUSED)
    try:
        tok = auth.google_login(email, claims.get("name") or email, set(WORKSPACES))
    except PermissionError as e:
        raise HTTPException(401, str(e))
    save_users()
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
    return {**u.public(), "support_contact": os.environ.get("AVOCET_SUPPORT_CONTACT", "your administrator")}


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
            "documents": svc.documents(w), "held": list(w.held.values()),
            "runs": [{"id": r["id"], "created_at": r["created_at"], "created_by": r["created_by"]} for r in w.runs.values()]}


@app.post("/v1/workspaces/{wid}/uploads")
async def upload(wid: str, kind: str = Form("auto"), data_class: str = Form("synthetic"), hint: str | None = Form(None),
                 file: UploadFile = File(...), u=Depends(current_user)):
    """kind="auto" (default): the server works out what the file is from its content and refuses
    anything that is not a recognised finance record. `hint` (from the file name) only breaks ties."""
    blob = await file.read(MAX_BYTES + 1)
    return handle(svc.upload, u, wid, kind, file.filename or "upload", blob, data_class, hint=hint)


@app.post("/v1/workspaces/{wid}/held-files")
async def hold_file(wid: str, data_class: str = Form("synthetic"), file: UploadFile = File(...), u=Depends(current_user)):
    """Any document type: stored unopened and marked for later conversion to JSON."""
    blob = await file.read(MAX_BYTES + 1)
    return handle(svc.hold_file, u, wid, file.filename or "upload", blob, data_class)


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


def _samples_on():
    """Built-in fixture files are for automated tests and developers only (AVOCET_SHOW_SAMPLES=1).
    Real use, and the demo, imports files the user uploads from their device or a connector."""
    if os.environ.get("AVOCET_SHOW_SAMPLES", "0") != "1":
        raise HTTPException(404, "not found")


@app.post("/v1/workspaces/{wid}/samples/load")
def load_sample(wid: str, u=Depends(current_user)):
    _samples_on()
    return handle(svc.load_sample, u, wid)


@app.get("/v1/workspaces/{wid}/samples/{name}")
def sample_file(wid: str, name: str, u=Depends(current_user)):
    _samples_on()
    handle(svc.gate, u, wid, "workspace.read")
    if not name.startswith(wid + "_") or "/" in name or name.endswith(".json") or not (FIXTURES / name).is_file():
        raise HTTPException(404, "not found")
    return FileResponse(FIXTURES / name, filename=name)


def _run_view(r: dict) -> dict:
    """A run as the UI sees it. Stage status is recomputed so 'Complete' always reflects open cases."""
    out = {k: v for k, v in r.items() if k != "batch_key"}
    if r.get("workflow"):
        out["stages"] = svc._stages(r["workflow"], r["result"])
    return out


@app.post("/v1/workspaces/{wid}/reconciliation-runs")
def create_run(wid: str, u=Depends(current_user)):
    return _run_view(handle(svc.run, u, wid))


@app.get("/v1/workspaces/{wid}/runs/{run_id}")
def get_run(wid: str, run_id: str, u=Depends(current_user)):
    w = handle(svc.gate, u, wid, "workspace.read", run_id=run_id)
    if run_id not in w.runs:
        raise HTTPException(404, "not found")
    return _run_view(w.runs[run_id])


@app.get("/v1/workspaces/{wid}/trustops/proof")
def trustops_proof(wid: str, u=Depends(current_user)):
    """The four TrustOps decisions a CPA should see, each taken from the signed evidence log."""
    def role_of(uid):
        x = auth.users.get(uid)
        return x.role if x else None
    return handle(svc.trustops_proof, u, wid, role_of)


@app.post("/v1/workspaces/{wid}/runs/{run_id}/exceptions/{exc_id}/decisions")
def decide(wid: str, run_id: str, exc_id: str, body: dict = Body(...), u=Depends(current_user)):
    return handle(svc.decide, u, wid, run_id, exc_id, body.get("decision"), body.get("note", ""))


@app.post("/v1/workspaces/{wid}/runs/{run_id}/exceptions/{exc_id}/proposals")
def propose(wid: str, run_id: str, exc_id: str, body: dict = Body(...), u=Depends(current_user)):
    return handle(svc.propose, u, wid, run_id, exc_id, body.get("note", ""))


@app.post("/v1/workspaces/{wid}/actions/attempt")
def attempt(wid: str, body: dict = Body(...), u=Depends(current_user)):
    """Security demonstration: ask the gate for an action, as a person or as an agent
    (`as_agent`: "AG-03"). Never executes anything; every answer is signed into the evidence chain."""
    action = body.get("action", "")
    kw = {"data_class": body.get("data_class", "synthetic"), "model_route": body.get("model_route", "deterministic")}
    try:
        if body.get("as_agent"):
            r = svc.agent_attempt(u, wid, body["as_agent"], action, **kw)
            return JSONResponse(status_code=403 if r["decision"] == "DENY" else 200, content=r)
        svc.gate(u, wid, action, maker_id=body.get("maker_id"), **kw)
    except Denied as d:
        return JSONResponse(status_code=404 if d.conceal else 403,
                            content={"decision": "DENY", "reason_code": d.code, "policy_id": d.policy_id, "executed": False})
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"decision": "ALLOW", "executed": False, "note": "Gate allowed the request; this endpoint never executes."}


@app.get("/v1/templates/{kind}.csv")
def template(kind: str, u=Depends(current_user)):
    """A blank-but-for-two-examples CSV showing the columns LedgerSight expects for each file type."""
    if kind not in ingest.TEMPLATES:
        raise HTTPException(404, "not found")
    names = {"bank": "bank_statement", "ledger": "general_ledger", "ar": "open_invoices", "ap": "vendor_bills",
             "documents": "supporting_documents", "parties": "customers_and_vendors"}
    return Response(ingest.template_csv(kind), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="template_{names[kind]}.csv"'})


@app.get("/v1/workspaces/{wid}/sources")
def sources(wid: str, u=Depends(current_user)):
    return handle(svc.sources, u, wid)


@app.get("/v1/workspaces/{wid}/samples")
def sample_list(wid: str, u=Depends(current_user)):
    _samples_on()
    return handle(svc.sample_files, u, wid)


@app.get("/v1/workspaces/{wid}/runs/{run_id}/reports/{kind}.{fmt}")
def report(wid: str, run_id: str, kind: str, fmt: str, u=Depends(current_user)):
    rep, blob = handle(svc.report, u, wid, run_id, kind, fmt)
    if fmt == "json" and kind:
        return Response(blob, media_type=REPORT_TYPES[fmt])
    return Response(blob, media_type=REPORT_TYPES[fmt],
                    headers={"Content-Disposition": f'attachment; filename="{rep["report_id"]}.{fmt}"'})


@app.get("/v1/workspaces/{wid}/audit-trail.{fmt}")
def audit_trail(wid: str, fmt: str, u=Depends(current_user)):
    rep, blob = handle(svc.audit_trail, u, wid, fmt)
    return Response(blob, media_type=REPORT_TYPES[fmt],
                    headers={"Content-Disposition": f'attachment; filename="{rep["report_id"]}.{fmt}"'})


@app.post("/v1/workspaces/{wid}/ask")
def ask(wid: str, body: dict = Body(...), u=Depends(current_user)):
    return handle(svc.ask, u, wid, body.get("question", ""))


@app.get("/v1/workspaces/{wid}/ask/suggestions")
def ask_suggestions(wid: str, u=Depends(current_user)):
    return handle(svc.ask_suggestions, u, wid)


@app.post("/v1/workspaces/{wid}/profile")
def update_profile(wid: str, body: dict = Body(...), u=Depends(current_user)):
    return handle(svc.update_profile, u, wid, body)


@app.get("/v1/workspaces/{wid}/agents")
def agents(wid: str, u=Depends(current_user)):
    return handle(svc.agents_status, u, wid)


@app.get("/v1/rulebooks")
def rulebooks(u=Depends(current_user)):
    return svc.rulebooks()


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
