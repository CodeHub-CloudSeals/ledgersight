"""Google sign-in: domain-restricted, auto-provisions AVOCET_GOOGLE_DEFAULT_ROLE (preparer by
default) for any new account, replaces password + authenticator code for anyone already on file."""
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
    assert "CloudSeals" in r.json()["detail"]


def test_rejects_unverified_email(google_on, monkeypatch):
    monkeypatch.setattr(appmod.google_id_token, "verify_oauth2_token", fake_verify(claims(verified=False)))
    r = TestClient(appmod.app).post("/v1/login/google", json={"credential": "t"})
    assert r.status_code == 403


def test_never_seen_email_is_auto_provisioned_as_preparer_by_default(google_on, monkeypatch):
    monkeypatch.setattr(appmod.google_id_token, "verify_oauth2_token",
                         fake_verify(claims(email="new-person@cloudseals.com", name="New Person")))
    c = TestClient(appmod.app)
    r = c.post("/v1/login/google", json={"credential": "t"})
    assert r.status_code == 200, r.text
    me = c.get("/v1/me").json()
    assert me["user_id"] == "new-person@cloudseals.com"
    assert me["display_name"] == "New Person"
    assert me["role"] == "preparer"
    assert me["active"] is True


def test_default_role_can_be_cpa_reviewer_for_every_google_user(google_on, monkeypatch):
    """AVOCET_GOOGLE_DEFAULT_ROLE=owner_reviewer: everyone who signs in with Google can approve."""
    monkeypatch.setenv("AVOCET_GOOGLE_DEFAULT_ROLE", "owner_reviewer")
    monkeypatch.setattr(appmod.google_id_token, "verify_oauth2_token",
                         fake_verify(claims(email="reviewer-by-default@cloudseals.com", name="R")))
    c = TestClient(appmod.app)
    assert c.post("/v1/login/google", json={"credential": "t"}).status_code == 200
    assert c.get("/v1/me").json()["role"] == "owner_reviewer"


@pytest.mark.parametrize("value", ["support", "admin", "", "OWNER_REVIEWER"])
def test_unknown_default_role_falls_back_to_preparer(google_on, monkeypatch, value):
    monkeypatch.setenv("AVOCET_GOOGLE_DEFAULT_ROLE", value)
    monkeypatch.setattr(appmod.google_id_token, "verify_oauth2_token",
                         fake_verify(claims(email=f"fallback-{value or 'empty'}@cloudseals.com".lower(), name="F")))
    c = TestClient(appmod.app)
    assert c.post("/v1/login/google", json={"credential": "t"}).status_code == 200
    assert c.get("/v1/me").json()["role"] == "preparer"


def test_pre_registered_email_keeps_its_existing_role_not_auto_upgraded(google_on, monkeypatch):
    appmod.auth.invite("rajani@cloudseals.com", "Rajani", "preparer", {"client-a"})
    monkeypatch.setattr(appmod.google_id_token, "verify_oauth2_token",
                         fake_verify(claims(email="rajani@cloudseals.com", name="Rajani G")))
    c = TestClient(appmod.app)
    r = c.post("/v1/login/google", json={"credential": "t"})
    assert r.status_code == 200, r.text
    me = c.get("/v1/me").json()
    assert me["user_id"] == "rajani@cloudseals.com"
    assert me["display_name"] == "Rajani G"
    assert me["role"] == "preparer"
    assert me["active"] is True


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
