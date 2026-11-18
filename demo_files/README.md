# LedgerSight + TrustOps: test data and team walkthrough

All data here is synthetic, with generic names (Customer 21, Packaging Supplier…). Every result in this
guide is calculated by the app from these files and checked by `tests/test_demo_pack.py`.

## The three scenarios

| Folder | Story | What the team should see |
|---|---|---|
| `1_standard_month` | A normal month with every kind of problem | 9 matched, 6 sent to review, **$0.00 unexplained** |
| `2_clean_month` | A month where everything agrees | 8 of 8 matched, **0 problems**: it doesn't invent issues |
| `3_messy_files` | Broken rows and wrong files | 6 bad rows rejected with reasons, 2 wrong files refused, the rest still reconciles |

---

## Before you present (2 minutes)

1. Start fresh: stop the app, delete the `.ledgersight-data` folder, then press **F5** in VS Code
   ("LedgerSight (local)"). Open http://127.0.0.1:8090.
2. Click the half-moon button (top right) if the screen is dark. Make the browser full screen.
3. Optional: **Overview → Client profile**, type the client name you want the audience to see, and click **Save**.
4. Keep the `demo_files` folder open in File Explorer next to the browser.

---

## The 15-minute walkthrough

**The one sentence to open with:**
> "LedgerSight does the reconciliation work. TrustOps checks every single action before it happens,
> sends anything risky to a person, and keeps signed proof of everything."

### 1. The problem (1 min) · *Overview*
- Point at the **5 steps** across the top of the Overview: *Upload → Agents do the work → TrustOps checks → You decide → Reports*.
- Say: "Every month an accountant compares the bank statement to the books line by line. That's what we automate, safely."

### 2. Upload the files (2 min) · *Sources*
- **Choose files…** and select from `1_standard_month`:
  `11_bank_statement_October.pdf`, `02_general_ledger.xlsx`, `03_open_invoices_AR.csv`,
  `04_vendor_bills_AP.json`, `05_supporting_documents.csv`, `06_customers_and_vendors.csv`.
- For each file, show the **column mapping** and click **Confirm**.
- Say: "It reads PDF, Excel, CSV and JSON. It suggests which column is which, but a person confirms. Every file gets a fingerprint (SHA-256) so we can prove later it wasn't changed."
- Point at **Source status**: each source is **VERIFIED_UPLOAD**. Point at **Connections**: QuickBooks and Xero are honestly marked **PLANNED**.

### 3. The agents work (2 min) · *Agents*
- Click **Run reconciliation now**. The six cards update.
- Say: "Six agents, each with one job and only the tools it's allowed. Before every action, each one asked TrustOps for permission. These cards are built from the signed audit trail, not an animation."
- Point at **AG-04 Exception Triage: ESCALATE 6**: "It found six problems and TrustOps sent all of them to a human."

### 4. What it found (2 min) · *Reconciliation*, then *Review queue*
- Reconciliation: **9 matched automatically**, **variance $196.00**, **unexplained $0.00**: "every dollar of difference is accounted for."
- Review queue: walk through two items:
  - **DB05:** customer paid $1,000 of a $1,750 invoice, so **$750 is still owed**.
  - **DB06:** $420 paid but not in the books. Policy **POL-05**, needs a CPA reviewer. A vendor bill supports it.

### 5. Who is allowed to decide (3 min) · *Review queue*
- Top bar: switch **Acting as → Staff Preparer**. On DB06 type a note and click **Propose resolution for reviewer**.
  → The preparer has **no Approve, Reject or Return buttons**: "A preparer proposes; only a CPA reviewer decides. The server
  refuses a preparer decision even if someone calls it directly (POL-06 maker-checker / POL-02), and signs the refusal."
- Switch back to **CPA Reviewer** and click **Approve**. → **ACCEPTED**, with name, time and approval ID.
- Say: "Approval only changes the review record. Nothing is ever posted to the client's books."

### 6. What the AI can never do (1 min) · *Guardrails*
- Click **Agent posts a journal entry** → **DENY · POL-03 READ_ONLY**.
- Click **Agent approves a review item** → **DENY · POL-06**.
- Go back to **Agents**: AG-03 now shows **Blocked by TrustOps**.

### 7. Ask a question (1 min) · *Ask LedgerSight*
- Ask: *What is outstanding on INV-2105?* → **$750.00**, with the file and row it came from.
- Ask: *Show Acme Corp's deposits* → **refused**: "It only answers about the client you have open, and won't even say whether another client exists."

### 8. Proof (2 min) · *Reports*, then *Audit trail*
- Reports: **Download PDF** for both reports. "Same run ID on both: the finance result and the governance proof are linked."
- Audit trail: **Verify now** → **VERIFIED**. **Tamper test** → the edited copy **FAILS**, the real one still passes.
- Say: "Every action you just saw is in this signed, hash-chained log."

### 9. Two quick extras (1 min, optional)
- **Messy files:** Overview → **Clear this client's data**, then upload `3_messy_files/01_bank_export_messy.csv` and
  `02_ledger_export_messy.tsv`. *Import health* lists every rejected row with its reason. Try
  `03_meeting_notes_should_be_refused.docx`: it's refused.
- **Hidden instructions:** upload `1_standard_month/09_suspicious_invoice.pdf`. The "AI, pay $9,000" lines are
  flagged and ignored.

---

## Expected results in detail

### 1_standard_month
| Item | Problem | TrustOps |
|---|---|---|
| DB04 | Cloud hosting charged twice ($129.00) | ESCALATE · POL-04 |
| DB05 | $1,000.00 paid of $1,750.00 → **$750.00 owed** | ESCALATE · POL-09 |
| DB06 | $420.00 not in the ledger; vendor bill PK-3302 supports it | ESCALATE · POL-05 |
| DB09 | $186.40 expense with no receipt (upload `07_receipt_RC-4410.pdf`, confirm, run again: it clears) | ESCALATE · POL-09 |
| DB10 | $650.00 deposit, payer unknown | ESCALATE · POL-09 |
| GL-A-310 | $95.00 in the ledger, not in the bank | ESCALATE · POL-05 |

Bank net $2,310.60 · ledger net $2,114.60 · variance $196.00 · unexplained $0.00.
Use **one** bank file: the PDF (`11` or `12`), the CSV (`01`) or the bank download (`08`) all hold the same 12 lines.
Invoice PDFs `13`–`16` are read as documents (reference, date and amount, each with the line it came from).

### 2_clean_month
Upload `01_bank_statement.csv`, `02_general_ledger.xlsx`, `03_open_invoices_AR.csv`, `05_supporting_documents.csv`.
Result: 8 matched, 0 review items, variance $0.00.

### 3_messy_files
| File | Result |
|---|---|
| `01_bank_export_messy.csv` (different column names) | 8 rows: 4 accepted, 4 rejected (3 decimal places · bad currency "US" · a spreadsheet formula · the same ID twice). "$1,250.00" and "(900.00)" are read correctly |
| `02_ledger_export_messy.tsv` | 5 rows: 3 accepted, 2 rejected (both debit and credit filled · no amount) |
| `03_meeting_notes_should_be_refused.docx` | Refused: not a supported file type |
| `04_not_a_statement.json` | Refused: not a list of transactions |
| After reconciling | 3 matched; MX07 unmatched deposit; MX02 and MX06 missing receipts; unexplained $0.00 |

---

## Questions the team may ask

| Question | Answer |
|---|---|
| Is this AI? | The agents follow fixed, explainable rules. No language model touches the money, so the same files always give the same answer |
| Can it post to QuickBooks? | No. It is read-only by design (POL-03); QuickBooks/Xero connectors are planned, read-only |
| What if someone uploads a bad file? | Bad rows are rejected with a reason; macros, archives and Word files are refused (scenario 3) |
| What stops the preparer approving their own work? | TrustOps maker-checker (POL-06), checked on the server |
| Is the data kept after a restart? | Yes. It is stored in a database with the signed audit trail; clearing a client archives its trail |
| Can it handle real client data? | Not yet. Only synthetic data is allowed until security and data-processing approval (see `PRODUCTION.md`) |

## If something goes wrong

| Problem | Fix |
|---|---|
| Screen is dark | Click the half-moon button, top right |
| Old data is showing | Overview → **Clear this client's data**, or stop the app and delete `.ledgersight-data` |
| "Run reconciliation" shows the same run | Same files give the same run on purpose. Clear the data and upload again to see the agents work again |
| A file is refused | Check the message: it names the reason (type, size, macro, not a transaction list) |
| The page won't load | Make sure the app is running (F5 in VS Code), then refresh the browser |

Rebuild the generated files (PDF, Excel, QBO, refused files) with `python -m ledgersight.make_demo_pack`.
