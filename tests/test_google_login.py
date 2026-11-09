"""Google sign-in: invite-only, domain-restricted, replaces password + authenticator code."""
import pytest
from fastapi.testclient import TestClient

from test_ledgersight import appmod  # noqa: F401  (shared helper: safe isolated env vars are set on import)

CLIENT_ID = "test-client-id.apps.googleusercontent.com"


@pytest.fixture
def google_on(monkeypatch):
    monkeypatch.setattr(appmod, "_GL", True)
    monkeypatch.setattr(appmod, "GOOGLE_LOGIN_CLIENT_ID", CLIENT_ID)
    monkeypatch.setattr(appmod, "GOOGLE_LOGIN_DOMAIN", "cloudseals.com")


def claims(email="sam@cloudseals.com", hd="cloudseals.com", name="Sam", verified=True):
    return {"sub": "g-123", "email": email, "email_verified": verified, "hd": hd, "name": name}


def fake_verify(returns):
    def _verify(token, request, audience):
        assert audience == CLIENT_ID
        if isinstance(returns, Exception):
            raise returns
        return returns
    return _verify


def test_disabled_when_not_configured():
    r = TestClient(appmod.app).post("/v1/login/google", json={"credential": "x"})
    assert r.status_code == 404


def test_rejects_invalid_token(google_on, monkeypatch):
    monkeypatch.setattr(appmod.google_id_token, "verify_oauth2_token", fake_verify(ValueError("bad token")))
    r = TestClient(appmod.app).post("/v1/login/google", json={"credential": "garbage"})
    assert r.status_code == 401


def test_rejects_wrong_domain(google_on, monkeypatch):
    monkeypatch.setattr(appmod.google_id_token, "verify_oauth2_token",
                         fake_verify(claims(email="person@gmail.com", hd=None)))
    r = TestClient(appmod.app).post("/v1/login/google", json={"credential": "t"})
    assert r.status_code == 403
    assert "cloudseals.com" in r.json()["detail"]


def test_rejects_unverified_email(google_on, monkeypatch):
    monkeypatch.setattr(appmod.google_id_token, "verify_oauth2_token", fake_verify(claims(verified=False)))
    r = TestClient(appmod.app).post("/v1/login/google", json={"credential": "t"})
    assert r.status_code == 403


def test_no_invite_on_file_is_rejected(google_on, monkeypatch):
    monkeypatch.setattr(appmod.google_id_token, "verify_oauth2_token",
                         fake_verify(claims(email="nobody-invited@cloudseals.com")))
    r = TestClient(appmod.app).post("/v1/login/google", json={"credential": "t"})
    assert r.status_code == 401
    assert "invitation" in r.json()["detail"]


def test_invited_email_signs_in_without_password_or_mfa(google_on, monkeypatch):
    appmod.auth.invite("rajani@cloudseals.com", "Rajani", "preparer", {"client-a"})
    monkeypatch.setattr(appmod.google_id_token, "verify_oauth2_token",
                         fake_verify(claims(email="rajani@cloudseals.com", name="Rajani G")))
    c = TestClient(appmod.app)
    r = c.post("/v1/login/google", json={"credential": "t"})
    assert r.status_code == 200, r.text
    me = c.get("/v1/me")
    assert me.status_code == 200
    assert me.json()["user_id"] == "rajani@cloudseals.com"
    assert me.json()["display_name"] == "Rajani G"
    assert me.json()["active"] is True


def test_revoked_user_cannot_sign_in_with_google(google_on, monkeypatch):
    appmod.auth.invite("gone@cloudseals.com", "Gone", "preparer", {"client-a"})
    appmod.auth.revoke("gone@cloudseals.com")
    monkeypatch.setattr(appmod.google_id_token, "verify_oauth2_token",
                         fake_verify(claims(email="gone@cloudseals.com")))
    r = TestClient(appmod.app).post("/v1/login/google", json={"credential": "t"})
    assert r.status_code == 401


def test_auth_config_exposes_client_id_only_when_enabled(google_on):
    c = TestClient(appmod.app).get("/v1/auth-config").json()
    assert c["google_login"] is True
    assert c["google_client_id"] == CLIENT_ID


def test_auth_config_hides_google_login_by_default():
    c = TestClient(appmod.app).get("/v1/auth-config").json()
    assert c["google_login"] is False
    assert c["google_client_id"] is None
