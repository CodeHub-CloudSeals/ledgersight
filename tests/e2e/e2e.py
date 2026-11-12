"""End-to-end click-through of the LedgerSight UI (follows demo_files/README.md walkthrough).
Runs against isolated test servers: 8091 (no-login demo mode) and 8092 (login mode).
Usage: tests/e2e/run_all.sh [shots_dir]   (needs: pip install playwright; playwright install chromium)
Set E2E_CHANNEL=msedge to use the installed Edge instead of Playwright's Chromium."""
import json, os, re, sys, time
sys.stdout.reconfigure(encoding="utf-8")
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

BASE = "http://127.0.0.1:8091"; LOGIN_BASE = "http://127.0.0.1:8092"
DEMO = Path(__file__).resolve().parents[2] / "demo_files"
M1 = DEMO / "1_standard_month"
SHOTS = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent / "shots"); SHOTS.mkdir(parents=True, exist_ok=True)
RESULTS = []          # (feature, how, ok, detail)
ERRORS = []           # page JS errors / console errors

def check(feature, how, fn):
    try:
        detail = fn()
        RESULTS.append((feature, how, True, detail or ""))
    except Exception as e:  # noqa
        RESULTS.append((feature, how, False, f"{type(e).__name__}: {(str(e).splitlines() or [repr(e)])[0][:300]}"))

# records every state each pipeline node passes through (to prove the animation really ran)
OBSERVER = """
window.__seen = {}; window.__countSeen = new Set();
new MutationObserver(() => {
  document.querySelectorAll('#ov-agents .pnode, #ag-pipeline .pnode, #rc-pipeline .pnode').forEach(n => {
    const code = n.querySelector('.pcode')?.textContent; const st = [...n.classList].find(c => c.startsWith('s-'));
    if (code && st) (window.__seen[code] = window.__seen[code] || new Set()).add(st);
  });
  document.querySelectorAll('[data-pipe=count]').forEach(c => window.__countSeen.add(c.textContent));
}).observe(document.documentElement, {subtree: true, childList: true, attributes: true});
"""
HOOK = """
window.__seen = {}; window.__countSeen = new Set();
const __orig = window.renderPipelines;
window.renderPipelines = function () { const r = __orig.apply(this, arguments);
  document.querySelectorAll('#ag-pipeline .pnode').forEach(n => {
    const code = n.querySelector('.pcode').textContent; const st = [...n.classList].find(c => c.startsWith('s-'));
    (window.__seen[code] = window.__seen[code] || new Set()).add(st); });
  document.querySelectorAll('[data-pipe=count]').forEach(c => window.__countSeen.add(c.textContent)); return r; };
"""
def start(page, url):
    page.goto(url); page.wait_for_selector("#app:not(.hidden)"); page.wait_for_timeout(800); page.evaluate(HOOK)

seen = lambda p: p.evaluate("Object.fromEntries(Object.entries(window.__seen).map(([k,v])=>[k,[...v]]))")
reset_seen = lambda p: p.evaluate("window.__seen = {}; window.__countSeen = new Set();")

def tab(p, name):
    p.click(f'.tab[data-tab="{name}"]'); p.wait_for_timeout(350)

def shot(p, name):
    p.screenshot(path=str(SHOTS / f"{name}.png"), full_page=True)

def wire(page):
    page.on("pageerror", lambda e: ERRORS.append(f"pageerror: {e}"))
    page.on("console", lambda m: ERRORS.append(f"console.{m.type}: {m.text}") if m.type == "error" and "favicon" not in m.text else None)

def upload_and_map(p, fname):
    p.set_input_files("#up-file", str(M1 / fname))
    p.wait_for_selector("#mapping:not(.hidden) button.primary", timeout=15000)
    # 2026-11-12: every field says whether it is required; dates say how they will be read
    assert p.locator("#mapping .map-req").count() >= 1, "required fields not marked in the mapping step"
    p.click("#mapping button.primary")
    p.wait_for_selector("#mapping.hidden", state="attached", timeout=15000)
    p.wait_for_timeout(300)

def wait_idle(p):
    p.wait_for_function("S.anim === null && !document.querySelector('#run-btn').disabled && !document.querySelector('#ag-run').disabled", timeout=20000)
    p.wait_for_timeout(300)

def no_hscroll(p):
    w = p.evaluate("[document.documentElement.scrollWidth, window.innerWidth]")
    if w[0] > w[1] + 1:
        wide = p.evaluate("""[...document.querySelectorAll('main *, .topbar *, .stepper *')].filter(e => e.offsetParent && e.getBoundingClientRect().right > innerWidth + 1)
          .map(e => (e.id ? '#' + e.id : e.tagName.toLowerCase() + '.' + [...e.classList].join('.')) + ' w=' + Math.round(e.getBoundingClientRect().width)).slice(0, 6).join(' | ')""")
        tabname = p.evaluate("document.querySelector('.panel.active').id")
        raise AssertionError(f"{tabname}: scrollWidth {w[0]} > {w[1]}: {wide}")

with sync_playwright() as pw:
    br = pw.chromium.launch(channel=os.environ.get("E2E_CHANNEL") or None, headless=True)
    ctx = br.new_context(viewport={"width": 1280, "height": 900}, accept_downloads=True, bypass_csp=True)
    p = ctx.new_page(); wire(p)
    start(p, BASE + "/")

    # ---------- shell ----------
    def t_shell():
        groups = p.eval_on_selector_all(".navlabel", "n => n.map(x => x.textContent)")
        assert groups == ["WorkSpace", "Outputs", "Governance", "Admin"], groups
        assert p.is_visible("#ws-select") and p.is_visible("#act-as") and p.is_visible("#theme") and p.is_visible("#ask-open") and p.is_visible("#tb-search")
        tabs = p.eval_on_selector_all(".tab", "n => n.map(x => x.dataset.tab)")
        assert len(tabs) == 13 and "ask" not in tabs, tabs
        assert p.inner_text("#app-version").strip() == "LedgerSight v1.0.0", p.inner_text("#app-version")
        assert "Governed By TrustOps" in p.inner_text(".bsub")
        top = p.eval_on_selector("#tb-search", "e => e.closest('.topbar') !== null")
        fab = p.eval_on_selector("#ask-open", "e => { const r = e.getBoundingClientRect(); return [innerWidth - r.right, innerHeight - r.bottom]; }")
        assert top and fab[0] < 80 and fab[1] < 80, (top, fab)  # Ask: search box in the top bar + floating button bottom-right
        p.keyboard.press("Control+k"); p.wait_for_timeout(400); assert p.evaluate("S.askOpen"); p.keyboard.press("Escape"); p.wait_for_timeout(300)
        return f"WorkSpace/Outputs/Governance/Admin, {len(tabs)} tabs, Ask in top bar (Ctrl+K) and bottom-right, LedgerSight v1.0.0, Governed By TrustOps"
    check("App shell: grouped sidebar + top bar", "DOM check", t_shell)

    def t_empty_overview():
        expect(p.locator("#ag-pipeline .pnode")).to_have_count(6)
        assert p.is_visible("#ov-how"), "onboarding guide shown before any upload"
        assert p.locator("[data-pipe=count]").count() == 0 and "agents active" not in p.inner_text("body")
        assert p.locator("#ag-pipeline .pnode.s-idle").count() == 6
        shot(p, "01-overview-empty")
        return "6 agent nodes, all 'Not used yet', no agent counter"
    check("Overview pipeline (empty state)", "fresh data dir, DOM check", t_empty_overview)

    def t_readiness():
        tab(p, "recon"); assert p.is_disabled("#run-btn"); assert "before you can run" in p.inner_text("#recon-ready").lower()
        tab(p, "exceptions"); assert "Nothing to review yet" in p.inner_text("#exc-list")
        return "Run disabled until bank + ledger exist; empty states shown"
    check("Readiness guard + empty states", "click Reconciliation / Review queue before upload", t_readiness)

    # ---------- sources ----------
    tab(p, "import")
    def t_refuse_unsupported():
        p.set_input_files("#up-file", str(M1 / "10_should_be_rejected_macro.xlsm")); p.wait_for_timeout(1200)
        docx = DEMO / "3_messy_files" / "03_meeting_notes_should_be_refused.docx"
        p.set_input_files("#up-file", str(docx)); p.wait_for_timeout(1200)
        out = p.inner_text("#up-refused")
        assert "03_meeting_notes_should_be_refused.docx" in out, out
        assert p.locator("#mapping:not(.hidden)").count() == 0  # never parsed or mapped
        p.evaluate("put('up-refused')")
        return "macro workbook and Word file refused with the reason; nothing parsed or mapped"
    check("Unsupported formats refused", "upload a macro workbook and a Word file", t_refuse_unsupported)

    def t_uploads():
        reset_seen(p)
        for f in ["01_bank_statement.csv", "02_general_ledger.xlsx", "03_open_invoices_AR.csv", "04_vendor_bills_AP.json",
                  "05_supporting_documents.csv", "06_customers_and_vendors.csv"]:
            upload_and_map(p, f)
        s = seen(p)
        assert "s-working" in s.get("AG-01", []), f"AG-01 never shown working during upload: {s}"
        st = p.inner_text("#src-status"); assert st.lower().count("verified upload") >= 6, st
        h = p.inner_text("#health"); assert "balances" in h.lower()
        shot(p, "02-sources")
        return f"6 files mapped + imported; AG-01 states seen {s.get('AG-01')}; 6 'Verified Upload'"
    check("Upload + column-mapping confirm (6 files)", "set files on #up-file, click Confirm mapping & import", t_uploads)

    def t_connectors():
        c = p.inner_text("#connectors"); assert "planned" in c.lower(); return "QuickBooks/Xero shown Planned"
    check("Connections status labels", "read #connectors", t_connectors)

    def t_pdf():
        p.set_input_files("#up-file", str(M1 / "09_suspicious_invoice.pdf"))
        p.wait_for_selector("#mapping:not(.hidden)", timeout=20000)
        txt = p.inner_text("#mapping"); assert "instruction-like text" in txt.lower(), txt[:200]
        p.click("#mapping button.primary"); p.wait_for_timeout(800)
        return "PDF parsed in isolated worker; hidden instructions flagged; document confirmed"
    check("PDF upload + prompt-injection flag + confirm", "upload 09_suspicious_invoice.pdf", t_pdf)

    # ---------- reconcile with animation ----------
    def t_run():
        tab(p, "recon"); reset_seen(p)
        assert not p.is_disabled("#run-btn")
        p.click("#run-btn")
        p.wait_for_function("S.anim && S.anim.mode === 'replay' && document.querySelector('#rc-pipeline .s-working')", timeout=10000)
        p.wait_for_timeout(700); shot(p, "03-recon-animating")
        wait_idle(p)
        s = seen(p)
        for c in ["AG-01", "AG-02", "AG-03", "AG-04", "AG-05", "AG-06"]:
            assert "s-working" in s.get(c, []), f"{c} never animated: {s}"
        tiles = p.inner_text("#tiles")
        assert "$196.00" in tiles and "$0.00" in tiles, tiles
        n = p.locator("#rc-pipeline .pnode.s-escalated").inner_text()
        assert "AG-04" in n and "6 sent to a person" in n, n.replace(chr(10), " / ")
        shot(p, "04-recon-done")
        return "each of 6 agents went queued→working→done/escalated; variance $196.00, unexplained $0.00; AG-04 '6 sent to a person'"
    check("Run reconciliation + live pipeline replay", "click Run, watch pipeline states", t_run)

    def t_numbers():
        txt = p.inner_text("#rc-pipeline"); assert " ms" not in txt and "POL-02" not in txt and " rows" not in txt, txt
        tips = " ".join(p.eval_on_selector_all("#rc-pipeline .pline", "n => n.map(x => x.title)"))
        run = p.evaluate("S.run.workflow.map(s => [s.agent, s.rows_in, s.rows_out, s.duration_ms])")
        for a, i, o, ms in run:
            assert f"{i} → {o} rows" in tips and f"{ms} ms" in tips, (a, i, o, ms)
        r = p.evaluate("[S.run.result.matches.length, S.run.result.exceptions.length]")
        assert f"{r[0]} matched · {r[1]} for review" in txt and "files checked" in txt and "reports ready" in txt, txt
        return f"cards show business outcomes ({r[0]} matched · {r[1]} for review); rows, ms, policy in tooltip = run response"
    check("Pipeline numbers = server data (no invented values)", "compare DOM text with S.run.workflow", t_numbers)

    def t_matches():
        rows = p.locator("#matches tr.click"); assert rows.count() == 9, rows.count()
        rows.first.click(); assert "Trace" in p.inner_text("#detail")
        assert "Step-by-step detail" not in p.inner_text("#p-recon")
        return "9 matched rows; source detail opens; no step-by-step panel"
    check("Matched table + source detail", "click first match", t_matches)

    # ---------- overview after run ----------
    def t_overview():
        tab(p, "home"); p.wait_for_timeout(600)
        k = p.inner_text("#ov-kpis") + " " + p.inner_text("#ov-var"); assert "9" in k and "$196.00" in k, k
        assert not p.is_visible("#ov-how"), "guide hidden once files are in"
        tab(p, "recon"); p.wait_for_timeout(400)
        assert p.locator("#rc-pipeline .pnode").count() == 6
        tops = p.eval_on_selector_all("#rc-pipeline .pnode", "n => new Set(n.map(x => Math.round(x.getBoundingClientRect().top))).size")
        assert tops == 1, f"pipeline wraps into {tops} rows at 1280px"
        nr = p.inner_text("#nav-review"); assert nr.strip() == "6", nr
        tab(p, "evidence"); p.wait_for_timeout(800)
        g = p.inner_text("#ov-gov"); esc = int(re.search(r"ESCALATE\s+(\d+)", g).group(1)); assert esc >= 6, g
        act = p.inner_text("#ov-activity"); assert "No activity yet" not in act and "AG-0" in act, act[:200]
        tab(p, "home"); shot(p, "05-overview")
        return f"KPIs from run; sidebar review count {nr}; ESCALATE {esc} and recent activity are current"
    check("Overview KPIs + review count badge", "Overview after run", t_overview)

    def t_decision_buttons():
        # Regression (2026-11-12): Reject/Approve kept the old square style next to the pill Propose/Return buttons.
        tab(p, "exceptions")
        st = p.evaluate("""() => { const row = [...document.querySelectorAll('.xa-row')].find(r => r.offsetParent); if (!row) return null;
            return [...row.querySelectorAll('button:not(.linkbtn)')].map(b => [b.textContent, Math.round(b.getBoundingClientRect().height), getComputedStyle(b).borderTopLeftRadius]); }""")
        if not st: return "no open case on screen; nothing to check"
        assert len({h for _, h, _ in st}) == 1 and {r for _, _, r in st} == {"999px"}, st
        return f"{len(st)} decision buttons, one height ({st[0][1]}px), all pill-shaped: " + ", ".join(t for t, _, _ in st)
    check("Decision buttons share one style", "Review queue: compare height and corner radius", t_decision_buttons)

    def t_page_motion():
        # 2026-11-12: page entrance (title + subtitle rise word by word, content follows) + liquid sidebar glide;
        # nothing left behind (word wrappers removed, no inline styles).
        tab(p, "home"); p.wait_for_timeout(600)
        if not p.evaluate("!!window.Motion"): return "motion library absent; static pages (fallback)"
        p.click('.sidebar nav .tab[data-tab="reports"]'); p.wait_for_timeout(60)
        mid = p.evaluate("""() => { const w = [...document.querySelectorAll('#p-reports .ls-wi')], pl = document.querySelector('.nav-pill').getBoundingClientRect();
            return [w.length, new Set(w.slice(0, 8).map(e => Math.round(new DOMMatrix(getComputedStyle(e).transform).m42))).size, Math.round(pl.height)]; }""")
        assert mid[0] >= 5 and mid[1] > 1, f"words are not rising one after another: {mid}"
        p.wait_for_timeout(2400)
        left = p.evaluate("""() => document.querySelectorAll('.ls-w, .ls-wrun, [data-ls-split]').length + [...document.querySelectorAll('.panel.active *')].filter(e => e.offsetParent &&
            ((e.style.opacity && e.style.opacity !== '1') || e.style.filter || (e.style.transform && e.style.transform !== 'none'))).length""")
        pill = p.evaluate("""() => { const P = document.querySelector('.nav-pill').getBoundingClientRect(), A = document.querySelector('.sidebar nav .tab.active').getBoundingClientRect();
            return Math.max(Math.abs(P.top - A.top), Math.abs(P.height - A.height)); }""")
        assert left == 0 and pill <= 1, (left, pill)
        return f"{mid[0]} words rising in sequence, sidebar highlight {mid[2]}px tall mid-glide; settles exactly, wrappers removed"
    check("Page entrance + sidebar glide", "switch Overview → Reports, sample mid-animation and after", t_page_motion)

    def t_workflow_stepper():
        # 2026-11-12: Agent workflow is one stepper: no per-step boxes, no "Done" badge next to the tick,
        # and the step that sent work to a person opens the review queue.
        tab(p, "agents"); p.wait_for_timeout(1500)
        st = p.evaluate("""() => { const t = document.querySelector('#ag-pipeline, #ag-pipe');
            const done = [...t.querySelectorAll('.pnode.s-done')], esc = t.querySelector('.pnode[data-go-review]');
            return [done.length, done.filter(n => n.querySelector('.pstate').offsetParent).length,
                    [...t.querySelectorAll('.pbody')].filter(b => parseFloat(getComputedStyle(b).borderTopWidth) > 0 || getComputedStyle(b).backgroundColor !== 'rgba(0, 0, 0, 0)').length,
                    esc ? esc.querySelector('.pstate').textContent : null]; }""")
        assert st[0] >= 1 and st[1] == 0 and st[2] == 0, st
        if st[3]:
            p.click("#ag-pipe .pnode[data-go-review]"); p.wait_for_timeout(700)
            assert p.evaluate("document.querySelector('#p-exceptions').classList.contains('active')"), "escalated step did not open the review queue"
        return f"{st[0]} finished steps show only the tick (no badge, no box); '{st[3]}' opens the review queue"
    check("Agent workflow stepper", "Agents page: status badges, step boxes, escalated link", t_workflow_stepper)

    def t_design_rules():
        # DESIGN_RULES.md: pill buttons, no edge stripes, borderless insets/cards, 42px fields, frameless tables
        js = "\n".join(l for l in open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "design_rules.js"), encoding="utf-8").read().splitlines()
                       if not l.lstrip().startswith("//"))
        bad, pages = [], ("home", "import", "recon", "exceptions", "parties", "trace", "reports", "agents", "rules", "controls", "evidence", "trust")
        for t in pages:
            tab(p, t)
            if t == "exceptions": p.evaluate("document.querySelectorAll('#exc-list details').forEach(d => d.open = true)")
            bad += [f"{t}: {x}" for x in p.evaluate(js)]
        assert not bad, bad[:12]
        return f"all {len(pages)} pages follow DESIGN_RULES.md (buttons, cards, inner boxes, fields, tables, status)"
    check("Design rules hold on every page", "tests/e2e/design_rules.js on each page", t_design_rules)


    # ---------- review queue: maker-checker ----------
    def exc(id_):
        return p.locator("#exc-list .exc", has_text=id_).first
    def t_maker_checker():
        tab(p, "exceptions"); assert p.locator("#exc-list .exc").count() == 6
        p.select_option("#act-as", label="Staff Preparer"); p.wait_for_timeout(1200); tab(p, "exceptions")
        e = exc("DB06"); e.locator("textarea").fill("Vendor bill PK-3302 supports it")
        e.get_by_role("button", name="Propose resolution for reviewer").click(); p.wait_for_timeout(700)
        e = exc("DB06"); e.get_by_role("button", name="Approve").click(); p.wait_for_timeout(700)
        out = exc("DB06").inner_text(); assert "MAKER_CHECKER" in out or "maker-checker" in out.lower(), out[-300:]
        assert "can't approve it yourself" in out, out[-300:]
        shot(p, "06-maker-checker")
        # 2026-11-12: each button gets its own refusal text (it used to say "approve" for Reject and Return too)
        exc("DB06").get_by_role("button", name="Reject").click(); p.wait_for_timeout(700)
        out = exc("DB06").inner_text(); assert "can't reject it yourself" in out and "approve it" not in out, out[-300:]
        exc("DB06").get_by_role("button", name="Return to preparer").click(); p.wait_for_timeout(700)
        out = exc("DB06").inner_text(); assert "Return to preparer is how a CPA reviewer" in out and "approve it" not in out, out[-300:]
        other = p.locator("#exc-list .exc").filter(has_not_text="DB06").filter(has=p.get_by_role("button", name="Reject")).first
        other.get_by_role("button", name="Reject").click(); p.wait_for_timeout(700)
        out = other.inner_text(); assert "Only a CPA reviewer can reject" in out and "you proposed" not in out.lower(), out[-300:]
        return "Preparer: own proposal DB06 → Approve/Reject/Return each refused by maker-checker in their own words; another case → 'Only a CPA reviewer can reject'"
    check("Maker-checker (preparer cannot approve own proposal)", "Acting as Staff Preparer → Propose → Approve", t_maker_checker)

    def t_decisions():
        p.select_option("#act-as", label="CPA Reviewer"); p.wait_for_timeout(1200); tab(p, "exceptions")
        exc("DB06").get_by_role("button", name="Approve").click(); p.wait_for_timeout(700)
        assert "ACCEPTED" in exc("DB06").inner_text()
        e = exc("DB04"); e.locator("textarea").fill("Duplicate confirmed with bank"); e.get_by_role("button", name="Reject").click(); p.wait_for_timeout(700)
        assert "REJECTED" in exc("DB04").inner_text()
        e = exc("DB10"); e.get_by_role("button", name="Return to preparer").click(); p.wait_for_timeout(700)
        assert "returned" in exc("DB10").inner_text().lower()
        return "DB06 ACCEPTED, DB04 REJECTED, DB10 RETURNED"
    check("Review decisions ACCEPT / REJECT / RETURN", "Acting as CPA Reviewer", t_decisions)

    def t_review_flow():
        tab(p, "exceptions")
        p.click(".exc-filter .pl-chip:has-text('Decided')"); p.wait_for_timeout(300)
        decided = p.locator("#exc-list .exc").count()
        p.click(".exc-filter .pl-chip:has-text('Open')"); p.wait_for_timeout(300)
        opened = p.locator("#exc-list .exc").count()
        assert decided == 2 and opened == 4, (decided, opened)  # DB06 accepted + DB04 rejected; DB10 returned is still open
        # Overview next step (reviewer) jumps to the first item that needs the reviewer, highlighted
        tab(p, "home"); nxt = p.inner_text("#next-step"); assert nxt.startswith("Review 3 items"), nxt
        p.click("#next-step"); p.wait_for_timeout(600)
        assert p.evaluate("document.querySelector('#p-exceptions').classList.contains('active')")
        assert p.locator("#exc-list .exc.flash.OPEN").count() == 1
        # Preparer sees what the preparer can do
        p.select_option("#act-as", label="Staff Preparer"); p.wait_for_timeout(1200); tab(p, "home")
        prep = p.inner_text("#next-step"); assert prep.startswith("Propose resolutions for"), prep
        p.select_option("#act-as", label="CPA Reviewer"); p.wait_for_timeout(1200)
        return f"Open {opened} / Decided {decided}; reviewer next step '{nxt.strip()}' jumps + highlights; preparer sees '{prep.strip()}'"
    check("Review flow: filters, role-aware next step, jump to item", "filter chips, Overview next step as each role", t_review_flow)

    def t_finish_period():
        tab(p, "exceptions")
        for _ in range(8):
            items = p.locator("#exc-list .exc.OPEN, #exc-list .exc.RETURNED")
            if items.count() == 0:
                break
            items.first.get_by_role("button", name="Approve").click(); p.wait_for_timeout(600)
        p.click(".exc-filter .pl-chip:has-text('Open')"); p.wait_for_timeout(300)
        assert "Every item has a decision" in p.inner_text("#exc-list")
        tab(p, "home"); assert p.inner_text("#next-step").startswith("Download the reports") and p.is_visible("#next-period")
        tab(p, "reports"); p.wait_for_selector("#rep-cards a.btnlink")
        p.evaluate("document.querySelectorAll('#rep-cards a.btnlink').forEach(a => a.addEventListener('click', e => e.preventDefault()))")
        p.locator("#rep-cards a.btnlink").first.click(); p.wait_for_timeout(700)
        assert p.locator("#stepper .step.done").count() == 5, p.inner_text("#stepper")
        return "all decided → 'Download the reports' + 'Start next period'; downloading a report completes step 5"
    check("Month-end: all decided, report step completes", "approve the rest, open Reports, download", t_finish_period)

    # ---------- guardrails ----------
    def t_guard():
        tab(p, "controls")
        p.locator("#attack-grid .card", has_text="Agent posts a journal entry").get_by_role("button", name="Attempt").click(); p.wait_for_timeout(600)
        p.locator("#attack-grid .card", has_text="Release a vendor payment").get_by_role("button", name="Attempt").click(); p.wait_for_timeout(600)
        out = p.inner_text("#attack-out"); assert out.count("DENY") >= 2 and "POL-03" in out, out
        tab(p, "agents"); p.wait_for_timeout(2500)
        n = p.locator("#ag-pipeline .pnode", has_text="AG-03").inner_text(); assert "blocked by trustops" in n.lower(), n
        shot(p, "07-agents-blocked")
        return "both refused (DENY, POL-03); AG-03 node shows Blocked by TrustOps"
    check("Guardrails + blocked agent in pipeline", "Attempt forbidden actions, then Agents tab", t_guard)

    def t_agents_tab():
        assert p.locator("#ag-grid .agent").count() == 6
        assert "live" in p.inner_text("#ag-live")
        assert p.locator("#ag-feed tr").count() > 5
        assert p.locator("#ag-grid .agent details").count() == 0 and "Allowed" in p.locator("#ag-grid .agent").first.inner_text()
        return "6 cards (no More details), live feed polling"
    check("Agents tab cards + live feed", "Agents tab", t_agents_tab)

    def t_agents_run():
        reset_seen(p); p.click("#ag-run")
        p.wait_for_function("S.anim !== null", timeout=10000); wait_idle(p)
        s = seen(p); assert all("s-working" in s.get(c, []) for c in ["AG-01", "AG-06"]), s
        return "Run from Agents tab animates the same pipeline"
    check("Run reconciliation now (Agents tab)", "click #ag-run", t_agents_run)

    # ---------- ask ----------
    def t_ask():
        tab(p, "reports"); p.click("#ask-open"); p.wait_for_selector("#ask-drawer.open"); p.wait_for_selector("#ask-chips .chip")
        p.wait_for_function("document.activeElement && document.activeElement.id === 'ask-q'", timeout=3000)
        p.fill("#ask-q", "What is outstanding on INV-2105?"); p.click("#ask-btn"); p.wait_for_timeout(900)
        a1 = p.inner_text("#ask-out .qa >> nth=-1"); assert "750.00" in a1, a1[:300]
        p.fill("#ask-q", "Show Acme Corp's deposits"); p.press("#ask-q", "Enter"); p.wait_for_timeout(900)
        a2 = p.inner_text("#ask-out .qa >> nth=-1"); assert "refused" in a2.lower(), a2[:300]
        assert p.evaluate("document.querySelector('#p-reports').classList.contains('active')")  # page stays where it was
        shot(p, "08-ask")
        p.keyboard.press("Escape"); p.wait_for_timeout(400)
        assert p.locator("#ask-drawer.open").count() == 0 and p.evaluate("document.querySelector('#ask-drawer').inert")
        return "opened from top-right over Reports; INV-2105 → $750.00 with sources; other-client question refused; Esc closes"
    check("Ask LedgerSight chat panel (top-right)", "click Ask button, ask, press Enter, Esc", t_ask)

    # ---------- reports ----------
    def t_reports():
        tab(p, "reports"); assert p.locator("#rep-cards .card").count() == 2
        p.locator("#rep-cards .card").first.get_by_role("button", name="Preview").click(); p.wait_for_timeout(800)
        assert "FIN-" in p.inner_text("#rep-prev-title")
        got = []
        for a in p.locator("#rep-cards a.btnlink").all():
            href = a.get_attribute("href"); r = p.request.get(BASE + href)
            assert r.status == 200 and len(r.body()) > 100, (href, r.status); got.append(href.rsplit("/", 1)[1])
        assert len(got) == 8, got
        # 2026-11-12: each card also links this run's review pack / the whole audit trail (no hunting on Audit trail)
        also = []
        for a in p.locator("#rep-cards .rep-also a").all():
            href = a.get_attribute("href"); r = p.request.get((BASE + href) if href.startswith("/") else href)
            assert r.status == 200, (href, r.status); also.append(a.inner_text())
        assert len(also) == 7, also
        shot(p, "09-reports")
        return "preview works; downloads 200: " + ", ".join(got) + f"; plus {len(also)} review-pack/audit-trail links"
    check("Reports: preview + 2 reports × 4 formats", "click Preview, fetch every download link", t_reports)

    # ---------- audit trail ----------
    def t_evidence():
        tab(p, "evidence"); p.click("#ev-refresh"); p.wait_for_timeout(600)
        b = p.inner_text("#verify-badge"); assert "VERIFIED" in b, b
        p.click("#tamper-btn"); p.wait_for_timeout(800); t = p.inner_text("#tamper-out"); assert "FAIL" in t and "PASS" in t, t
        for i in ["exp-csv", "exp-pack", "exp-print"]:
            href = p.get_attribute(f"#{i}", "href"); r = p.request.get(BASE + href); assert r.status == 200, (i, r.status)
        shot(p, "10-audit")
        return f"{b}; tamper copy FAIL, real chain PASS; CSV, JSON pack, printable pack all 200"
    check("Audit trail verify + tamper + exports", "Verify now, Tamper test, fetch exports", t_evidence)

    # ---------- counterparties / trace ----------
    def t_parties():
        tab(p, "parties"); p.wait_for_selector("#party-list tr.click"); p.locator("#party-list tr.click").first.click()
        p.wait_for_timeout(700); v = p.inner_text("#party-view"); assert "Transactions with" in v
        shot(p, "11-parties"); return "list + detail with transactions"
    check("Counterparties list + detail", "click first party", t_parties)

    def t_trace():
        tab(p, "trace"); p.wait_for_selector("#trace-list tr.click"); p.locator("#trace-list tr.click").first.click()
        p.wait_for_selector("#trace-view .timeline li"); assert p.locator("#trace-view .timeline li").count() >= 3
        shot(p, "12-trace"); return f"{p.locator('#trace-view .timeline li').count()} timeline steps + signed records"
    check("Trace a transaction", "click first bank line", t_trace)

    def t_rules_trust():
        tab(p, "rules"); p.wait_for_selector("#rules table"); r = p.inner_text("#rules"); assert "POL-01" in r and "POL-10" in r
        tab(p, "trust"); p.wait_for_timeout(300); t = p.inner_text("#trust"); assert "Never permitted" in t and "Ed25519" in t
        shot(p, "13-trust"); return "POL-01…POL-10 listed; data & security sections"
    check("Rulebooks + Data & security", "open tabs", t_rules_trust)

    def t_names():
        tab(p, "rules"); r = p.inner_text("#rules")
        for want in ("Client Scope", "Role Authority", "Ledger Post", "Upload Parse", "Query Answer"):
            assert want in r, want
        for raw in ("CLIENT_SCOPE", "ledger.post", "upload.parse"):
            assert raw not in r, raw
        tab(p, "agents"); p.wait_for_timeout(2300); feed = p.inner_text("#ag-feed")
        assert "agent:" not in feed and " Agent" in feed and "ALLOW · OK" in feed, feed[:300]
        tab(p, "evidence"); ev = p.inner_text("#events"); assert "upload.parse" not in ev and "Upload Parse" in ev
        return "Rulebooks, agent feed and audit trail show readable names (Client Scope, Ledger Post, Reconciliation Agent, ALLOW · OK)"
    check("Readable action, policy and agent names", "read Rulebooks, Agents feed, Audit trail", t_names)

    def t_colors():
        tab(p, "import")
        side = p.evaluate("getComputedStyle(document.querySelector('.sidebar')).backgroundColor")
        dz = p.evaluate("getComputedStyle(document.querySelector('.dropzone')).backgroundColor")
        conn = p.evaluate("getComputedStyle(document.querySelector('.dz-connect')).backgroundColor")
        assert side == dz == conn, (side, dz, conn)
        return f"sidebar, drop zone and connect row all {side}"
    check("Consistent menu / upload background", "compare computed colours", t_colors)

    # ---------- theme, profile, responsive ----------
    def t_theme():
        before = p.evaluate("getComputedStyle(document.body).backgroundColor"); p.click("#theme"); p.wait_for_timeout(200)
        after = p.evaluate("getComputedStyle(document.body).backgroundColor"); assert before != after, (before, after)
        tab(p, "home"); shot(p, "14-overview-dark"); p.click("#theme")
        return f"{before} → {after}"
    check("Light / dark theme toggle", "click #theme", t_theme)

    def t_profile():
        tab(p, "trust"); p.fill("#pf-name", "Client 1 (test)"); p.click("#pf-save"); p.wait_for_timeout(800)
        tab(p, "home"); p.wait_for_timeout(500)
        assert "Client 1 (test)" in p.inner_text("#ov-hero"); return "renamed and shown in header"
    check("Client profile save", "open profile, save name", t_profile)

    def t_responsive():
        for w in (1280, 390):
            p.set_viewport_size({"width": w, "height": 900})
            for t in ("home", "import", "recon", "agents", "exceptions", "parties", "trace", "reports", "rules", "controls", "evidence", "trust"):
                tab(p, t); no_hscroll(p)
            tab(p, "home"); shot(p, f"15-overview-{w}")
        p.set_viewport_size({"width": 1280, "height": 900}); return "no horizontal page scroll at 1280px and 390px on all 12 visible tabs"
    check("Responsive layout (1280px + phone)", "resize viewport, measure scrollWidth", t_responsive)

    def t_keyboard():
        tab(p, "home"); p.focus('.tab[data-tab="import"]'); p.keyboard.press("Enter"); p.wait_for_timeout(300)
        assert p.evaluate("document.querySelector('#p-import').classList.contains('active')")
        outline = p.evaluate("getComputedStyle(document.activeElement).outlineStyle")
        return f"Enter on focused tab opens it; focus outline: {outline}"
    check("Keyboard navigation", "focus a tab, press Enter", t_keyboard)

    def t_pill_after_peek():
        # Regression (2026-11-12): clicking a tab while the collapsed rail peeked open left the sliding highlight
        # on the expanded layout's coordinates (one item too low, wider than the rail) after it closed.
        p.set_viewport_size({"width": 1440, "height": 900}); tab(p, "home")
        was_mini = p.evaluate("document.body.classList.contains('side-mini')")
        if not was_mini: p.click("#side-toggle"); p.mouse.move(900, 500)
        p.wait_for_timeout(500)
        if not p.locator(".nav-pill").count(): return "no sliding highlight (motion library absent); nothing to check"
        seen = []
        rail_w = p.evaluate("document.querySelector('.sidebar').getBoundingClientRect().width")
        for t in ("reports", "agents", "evidence", "home"):
            el = p.locator(f'.sidebar nav .tab[data-tab="{t}"]'); el.hover(); p.wait_for_timeout(450)
            # 2026-11-12: the rail no longer expands over the page on hover; a label appears beside the icon instead
            w, tip = p.evaluate("[document.querySelector('.sidebar').getBoundingClientRect().width, document.getElementById('rail-tip').classList.contains('on')]")
            assert abs(w - rail_w) < 1 and tip, (t, w, rail_w, tip)
            el.click(); p.mouse.move(900, 500); p.wait_for_timeout(1200)
            d = p.evaluate("""() => { const P = document.querySelector('.nav-pill').getBoundingClientRect(),
                A = document.querySelector('.sidebar nav .tab.active').getBoundingClientRect(),
                S = document.querySelector('.sidebar').getBoundingClientRect();
                return [Math.max(Math.abs(P.left-A.left), Math.abs(P.top-A.top), Math.abs(P.width-A.width), Math.abs(P.height-A.height)), P.right <= S.right]; }""")
            assert d[0] <= 1 and d[1], (t, d); seen.append(t)
        if not was_mini: p.click("#side-toggle"); p.mouse.move(900, 500); p.wait_for_timeout(500)
        p.set_viewport_size({"width": 1280, "height": 900})
        return f"collapsed rail: hover shows a label (rail stays {round(rail_w)}px), click {', '.join(seen)} → highlight sits exactly on the active item"
    def t_tiles_aligned():
        # Regression (2026-11-12): wrapped tile labels pushed some numbers lower than others; 1280px left a 5+1 row.
        tab(p, "recon"); p.wait_for_timeout(1800); out = []  # let the page entrance finish before measuring
        for w in (1440, 1280, 390):
            p.set_viewport_size({"width": w, "height": 900}); p.wait_for_timeout(400)
            rows = p.evaluate("""() => { const m = {}; document.querySelectorAll('#tiles .tile').forEach(t => {
                const r = t.getBoundingClientRect(); (m[Math.round(r.top)] = m[Math.round(r.top)] || []).push(Math.round(t.querySelector('.v').getBoundingClientRect().bottom)); });
                return Object.values(m); }""")
            if not rows: return "no run on screen; nothing to check"
            assert all(len(set(r)) == 1 for r in rows), (w, rows)
            sizes = [len(r) for r in rows]; assert len(set(sizes)) == 1, (w, sizes); out.append(f"{w}px {'+'.join(map(str, sizes))}")
        p.set_viewport_size({"width": 1280, "height": 900})
        return "numbers on one line per row; " + ", ".join(out)
    def t_no_scrollbars():
        # Requested 2026-11-12: no visible scrollbars anywhere; boxes must still scroll.
        hits = []
        for t in ("home", "import", "recon", "agents", "rules", "evidence", "trust"):
            tab(p, t)
            hits += p.evaluate("""(t) => { const b = []; if (innerWidth - document.documentElement.clientWidth > 0) b.push(t + ':page');
              for (const e of document.querySelectorAll('body *')) { if (!e.offsetParent) continue; const cs = getComputedStyle(e);
                if (!/(auto|scroll)/.test(cs.overflowY + cs.overflowX)) continue;
                const bl = parseFloat(cs.borderLeftWidth) + parseFloat(cs.borderRightWidth), bt = parseFloat(cs.borderTopWidth) + parseFloat(cs.borderBottomWidth);
                if (e.offsetWidth - e.clientWidth - bl > .5 || e.offsetHeight - e.clientHeight - bt > .5) b.push(t + ':' + (e.id || e.className)); } return b; }""", t)
        assert not hits, hits
        tab(p, "evidence")
        n = p.evaluate("(() => { const e = document.getElementById('events'); if (e.scrollHeight <= e.clientHeight) return -1; e.scrollTop = 200; return e.scrollTop; })()")
        assert n != 0, "audit trail list no longer scrolls"
        return "no scrollbar on 7 pages; audit trail list still scrolls"

    check("No visible scrollbars", "measure scrollbar space on every scrolling box", t_no_scrollbars)

    check("Reconciliation number tiles line up", "measure tile values per row at 1440/1280/390px", t_tiles_aligned)

    check("Collapsed rail: label on hover, highlight after click", "collapse rail, hover (no expand), click tab, compare boxes", t_pill_after_peek)

    def t_back():
        tab(p, "home"); tab(p, "reports"); tab(p, "rules")
        p.go_back(); p.wait_for_timeout(400)
        assert p.evaluate("document.querySelector('#p-reports').classList.contains('active')"), "Back did not return to Reports"
        p.go_back(); p.wait_for_timeout(400)
        assert p.evaluate("document.querySelector('#p-home').classList.contains('active')"), "Back did not return to Overview"
        p.go_forward(); p.wait_for_timeout(400)
        assert p.evaluate("document.querySelector('#p-reports').classList.contains('active')")
        return "Back: Rulebooks → Reports → Overview; Forward → Reports"
    check("Browser Back / Forward between pages", "click pages, press Back twice, Forward", t_back)

    def t_reset():
        tab(p, "import"); p.click("#clear-all"); p.click(".dialog button.danger"); p.wait_for_timeout(1500)
        tab(p, "home"); assert p.locator("#ag-pipeline .pnode").count() == 6
        assert p.locator("#toasts .toast").count() <= 3
        assert "awaiting files" in p.inner_text("#ov-hero").lower(); return "confirm dialog → data cleared, back to empty state"
    check("Clear client data (with confirm dialog)", "Clear all imported files → confirm", t_reset)

    def t_no_loader():
        tab(p, "import")
        assert p.locator("#sample-btn").count() == 0 or not p.is_visible("#sample-btn")
        assert "load all files" not in p.inner_text("#p-import").lower()
        return "no built-in loader; files only come from the device, email or Drive"
    check("No built-in sample loader", "Sources page", t_no_loader)
    ctx.close()

    # ---------- reduced motion ----------
    ctx2 = br.new_context(viewport={"width": 1280, "height": 900}, reduced_motion="reduce", bypass_csp=True);     q = ctx2.new_page(); wire(q); start(q, BASE + "/#recon")
    def t_reduced():
        q.click('.tab[data-tab="import"]'); q.wait_for_timeout(300)
        for f in ("01_bank_statement.csv", "02_general_ledger.xlsx"):
            upload_and_map(q, f)
        q.click('.tab[data-tab="recon"]'); q.wait_for_timeout(400); q.click("#run-btn"); q.wait_for_timeout(900)
        anim = q.evaluate("getComputedStyle(document.querySelector('#rc-pipeline .pico')).animationName")
        done = q.locator("#rc-pipeline .pnode.s-done, #rc-pipeline .pnode.s-escalated").count()
        assert done == 6 and anim == "none", (done, anim); return "statuses update instantly; no CSS animation"
    check("prefers-reduced-motion respected", "context with reduced_motion=reduce, run", t_reduced)
    ctx2.close()

    # ---------- login mode: sign-in + People ----------
    ctx3 = br.new_context(viewport={"width": 1280, "height": 900}, bypass_csp=True); r = ctx3.new_page(); wire(r)
    def t_login():
        r.goto(LOGIN_BASE + "/"); r.wait_for_selector("#login:not(.hidden)"); shot(r, "16-login")
        r.fill("#lg-user", "presenter"); r.fill("#lg-pw", "wrong-password"); r.click("#lg-btn"); r.wait_for_timeout(700)
        assert r.inner_text("#lg-msg").strip(), "no error for wrong password"
        r.fill("#lg-pw", "E2e-Test-Password-42"); r.press("#lg-pw", "Enter"); r.wait_for_selector("#app:not(.hidden)")
        return "wrong password shows error; correct password signs in"
    check("Sign-in screen", "port 8092 login mode", t_login)
    def t_team():
        r.click('.tab[data-tab="team"]'); r.wait_for_timeout(500)
        r.fill("#inv-user", "staff@example.test"); r.fill("#inv-name", "Test Staff"); r.click("#inv-btn"); r.wait_for_timeout(700)
        assert "/invite" in r.inner_text("#inv-out"), r.inner_text("#inv-out")
        r.get_by_role("button", name="Revoke").first.click(); r.wait_for_timeout(700)
        assert "revoked" in r.inner_text("#users").lower(); shot(r, "17-people")
        r.click("#logout"); r.wait_for_selector("#login:not(.hidden)")
        return "invite link created, user revoked, sign out returns to login"
    check("People: invite, revoke, sign out", "login mode", t_team)
    ctx3.close(); br.close()

ok = sum(1 for x in RESULTS if x[2])
print(json.dumps({"passed": ok, "total": len(RESULTS), "results": RESULTS, "js_errors": ERRORS}, indent=1))
