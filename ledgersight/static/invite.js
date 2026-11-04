"use strict";
const token = new URLSearchParams(location.search).get("token") || "";
const $ = (id) => document.getElementById(id);
fetch("/v1/auth-config").then((r) => r.json()).then((c) => { if (!c.mfa) document.querySelectorAll(".mfa-only").forEach((n) => n.classList.add("hidden")); });
fetch(`/v1/invites/${encodeURIComponent(token)}`).then(async (r) => {
  const b = await r.json();
  if (!r.ok) { $("iv-msg").textContent = b.detail || "Invitation invalid"; $("iv-msg").className = "msg err"; $("iv-btn").disabled = true; return; }
  $("iv-title").textContent = `Welcome, ${b.display_name}`;
  $("iv-info").textContent = `User ID: ${b.user_id} · role: ${b.role} · access until ${new Date(b.account_expires_at).toLocaleDateString()}`;
  $("iv-secret").textContent = b.totp_secret;
});
$("iv-btn").onclick = async () => {
  const r = await fetch(`/v1/invites/${encodeURIComponent(token)}/accept`, { method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ password: $("iv-pw").value, code: $("iv-code").value.trim(), terms: $("iv-terms").checked }) });
  const b = await r.json();
  if (!r.ok) { $("iv-msg").textContent = b.detail; $("iv-msg").className = "msg err"; return; }
  $("iv-msg").textContent = "Activated. Redirecting to sign in…"; $("iv-msg").className = "msg good";
  setTimeout(() => (location.href = "/"), 1200);
};
