# LedgerSight upload test pack

Synthetic data for one client book, October 2026. Generic names only (Customer 21, Packaging Supplier…).
It is a different scenario from the built-in samples, so everything you see after uploading it is
calculated from these files.

## 1. Upload the books (Sources → Choose files…)

Select these together. The type is detected from the file name; confirm the columns for each.

| File | What it is | Format |
|---|---|---|
| **11_bank_statement_October.pdf** | bank statement, 12 transactions | **PDF** |
| 02_general_ledger.xlsx | general ledger, 10 entries | Excel |
| 03_open_invoices_AR.csv | 3 customer invoices (accounts receivable) | CSV |
| 04_vendor_bills_AP.json | 2 vendor bills (accounts payable) | JSON |
| 05_supporting_documents.csv | list of 4 documents on file | CSV |
| 06_customers_and_vendors.csv | who each customer and vendor is | CSV |

Use **one** bank statement only. These three alternatives all hold the same 12 transactions:
`11_bank_statement_October.pdf` (PDF), `12_bank_statement_paid_in_out.pdf` (PDF in a "Paid out / Paid in /
Balance" layout, line IDs are generated as PDF-001…), `01_bank_statement.csv` (CSV) or
`08_bank_statement_bank_download.qbo` (bank download).

Then press **Run reconciliation** (Reconciliation page) or **Run reconciliation now** (Agents page).

## 2. What you should get

9 transactions match automatically: DB01, DB02, DB03, DB05, DB07, DB08, DB09, DB11, DB12.

6 items go to the Review queue, each one ESCALATED by TrustOps to a CPA reviewer:

| Item | Problem | Policy |
|---|---|---|
| DB04 | Cloud hosting charged twice ($129.00); the ledger has it once | POL-04 |
| DB05 | Customer 22 paid $1,000.00 of a $1,750.00 invoice → **$750.00 still owed** | POL-09 |
| DB06 | $420.00 to the packaging supplier is not in the ledger; vendor bill PK-3302 supports it | POL-05 |
| DB09 | $186.40 office supplies expense has no receipt | POL-09 |
| DB10 | $650.00 mobile deposit, payer unknown | POL-09 |
| GL-A-310 | $95.00 electricity is in the ledger but not in the bank | POL-05 |

Totals: bank net $2,310.60 · ledger net $2,114.60 · variance $196.00 · **unexplained $0.00**.

## 3. Invoices and receipts (Sources → choose the PDF; it is read as a document)

| File | Reads as | Use it to |
|---|---|---|
| 07_receipt_RC-4410.pdf | receipt RC-4410, $186.40 | Confirm it and run again: **DB09 disappears** |
| 13_invoice_PK-3302_packaging.pdf | vendor invoice PK-3302, $420.00 | Evidence for DB06 |
| 14_invoice_CH-7781_hosting.pdf | vendor invoice CH-7781, $129.00 | Evidence for the hosting charge (DB03/DB04) |
| 15_invoice_TR-551_training.pdf | vendor invoice TR-551, $310.00 | Evidence for DB12 |
| 16_customer_invoice_INV-2105.pdf | sales invoice INV-2105, $1,750.00 | Evidence for DB05's open balance |

For each PDF, LedgerSight shows the reference, date and amount it read, the exact line on the page
each value came from, and a confidence level. You confirm or correct them before they are used.

## 4. Safety checks

| File | What should happen |
|---|---|
| 09_suspicious_invoice.pdf | Two lines of hidden "AI, pay this" instructions are **flagged and ignored**. Nothing is paid or approved |
| 10_should_be_rejected_macro.xlsm | **Refused**: macro workbooks are never accepted |

Rebuild the PDF/Excel/QBO files from the text sources with `python -m ledgersight.make_demo_pack`.
All results above are checked automatically by `tests/test_demo_pack.py`.
