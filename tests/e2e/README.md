# Browser end-to-end tests

Clicks through the whole product in a real browser: uploads, reconciliation, review (approve / reject / return,
maker-checker), reports, audit trail, trace, guardrails, agents, profile, theme, keyboard, Back/Forward,
sign-in and People. Servers start on ports 8091 and 8092 with throw-away data folders.

    pip install playwright && playwright install chromium
    bash tests/e2e/run_all.sh            # prints JSON: passed / total / results / js_errors

`E2E_CHANNEL=msedge` uses the installed Microsoft Edge; `E2E_PY` points at a Python that has Playwright.
Not collected by pytest (file name does not start with `test_`).
