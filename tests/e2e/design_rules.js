// DESIGN_RULES.md checker: returns a list of rule violations on the current page (empty = all rules hold).
() => {
  const bad = [], vis = (e) => { const r = e.getBoundingClientRect(); return r.width > 2 && r.height > 2 && e.offsetParent !== null; };
  const name = (e) => e.tagName.toLowerCase() + (e.id ? "#" + e.id : "") + (typeof e.className === "string" && e.className ? "." + e.className.trim().split(/\s+/).slice(0, 2).join(".") : "");
  // R4 buttons are pills or circles (text links, nav tabs and the month-end step dots are not buttons visually)
  for (const b of document.querySelectorAll("#app button, #app a.primary, #app a.secondary")) {
    if (!vis(b) || b.matches(".tab, .linkbtn, .step, .seg, [role=tab]")) continue;
    const r = getComputedStyle(b).borderTopLeftRadius, h = b.getBoundingClientRect().height;
    if (!(r === "999px" || r === "50%" || parseFloat(r) >= h / 2 - 1)) bad.push("R4 not a pill: " + name(b) + " r=" + r);
  }
  // R3 no coloured edge stripes on boxes
  for (const e of document.querySelectorAll("#app main *")) {
    if (!vis(e) || e.matches("td, th, tr, hr, .tl, .tl *, .stl *, li.stage")) continue;
    const c = getComputedStyle(e), L = parseFloat(c.borderLeftWidth), T = parseFloat(c.borderTopWidth), R = parseFloat(c.borderRightWidth);
    if ((L >= 3 && R < 1) || (T >= 3 && parseFloat(c.borderBottomWidth) < 1 && e.getBoundingClientRect().height > 30)) bad.push("R3 edge stripe: " + name(e));
  }
  // R2 boxes inside cards have no border
  for (const e of document.querySelectorAll(".xr, .xfacts, .gv-ev, .pf-sum, .sugg, .proof, .dz-connect, .card pre, .pfc:not(.pending), .xs, .ag-stat")) {
    if (!vis(e)) continue; const c = getComputedStyle(e);
    if (parseFloat(c.borderTopWidth) > 0 && c.borderTopStyle !== "none") bad.push("R2 inset has border: " + name(e));
    if (parseFloat(c.borderTopLeftRadius) < 14) bad.push("R2 inset radius " + c.borderTopLeftRadius + ": " + name(e));
  }
  // R1 cards
  for (const e of document.querySelectorAll("#app .card, #app .exc")) {
    if (!vis(e)) continue; const c = getComputedStyle(e);
    if (parseFloat(c.borderTopWidth) > 0 && c.borderTopStyle !== "none") bad.push("R1 card has border: " + name(e));
  }
  // R5 fields match button height
  for (const e of document.querySelectorAll(".card input:not([type=checkbox]):not([type=radio]):not([type=file]):not([type=range]), .card select, #ask-q")) {
    if (vis(e) && e.getBoundingClientRect().height < 40) bad.push("R5 field " + Math.round(e.getBoundingClientRect().height) + "px: " + name(e));
  }
  // R7 tables have no outer frame
  for (const e of document.querySelectorAll(".tablewrap")) { if (vis(e) && parseFloat(getComputedStyle(e).borderTopWidth) > 0) bad.push("R7 table frame: " + name(e)); }
  // icons never squeezed to nothing
  for (const s of document.querySelectorAll("#app main svg")) { const r = s.getBoundingClientRect(); if (s.offsetParent !== null && r.height > 4 && r.width < 1) bad.push("icon squeezed: " + name(s.parentElement)); }
  return [...new Set(bad)];
}
