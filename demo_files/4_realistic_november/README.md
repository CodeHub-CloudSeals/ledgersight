# Brightline Dental Studio · November 2026 (realistic-looking mock data)

**Synthetic data only.** Every company, reference and amount here is invented. Names were chosen to look like a
real small business's books so a demo feels real; none of them is a real customer or supplier.

Upload these from your computer (Sources → Choose files, or drag them onto the drop area), confirm each column
mapping, then Reconciliation → **Run reconciliation**.

| File | Recognised as | Rows |
|---|---|---|
| `01_bank_statement_November.csv` | Bank statement | 14 |
| `02_general_ledger_November.xlsx` | General ledger | 10 |
| `03_open_invoices.csv` | Open invoices (AR) | 4 |
| `04_vendor_bills.csv` | Vendor bills (AP) | 3 |
| `05_supporting_documents.csv` | Supporting documents | 3 |
| `06_customers_and_vendors.csv` | Customer & vendor list | 12 |

## Expected result: 10 matched, 8 to review

| Bank line | What happened | Result |
|---|---|---|
| BK001 | Maple Ridge Clinics pays INV-2101 in full (8,450.00) | Matched |
| BK002 | November rent to Crestline Property (4,200.00) | Matched |
| BK003 | Cloudframe subscription (349.00), no receipt on file | Matched · **Missing receipt** review |
| BK004 | Same Cloudframe charge again next day | **Possible duplicate** |
| BK005 | Copperleaf Bakery pays 3,000.00 of a 5,000.00 invoice | Matched · **Partial receipt** (2,000.00 still open) |
| BK006 | Inkwell Print Works debit (1,185.50), bill on file but not booked | **Missing ledger entry** |
| BK007 | Payroll run via Northgate (18,650.00) | Matched |
| BK008 | Summit Trail Outfitters wire, reference written "INV 2108" | Matched (reference normalised) |
| BK009 | Paperhouse office supplies on card (612.40), no receipt | Matched · **Missing receipt** review |
| BK010 | Mobile deposit 2,750.00 with no reference | **Unknown deposit** |
| BK011 | Monthly bank fee (35.00) | Matched |
| BK012 | Lumen Stock Media (299.00), booked a day later | Matched |
| BK013 | Bluewater Analytics pays INV-2112 (9,800.00); sale shipped FL → CA | Matched · **Tax review** |
| BK014 | International wire to Thames Valley Components: USD 5,212.75 for a GBP 4,100.00 bill | **FX review** (no rate is invented) |

When LedgerSight is connected to TrustOps, the 8 review items go to **TrustOps → Approvals** as soon as the run
completes, each with the bank line, the matching line, why it was flagged and the suggested next step.
