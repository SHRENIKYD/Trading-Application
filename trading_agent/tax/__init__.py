"""Local crypto tax ledger for India: transactions in SQLite, FIFO cost basis, Schedule VDA export.

Rules implemented (confirm with a chartered accountant; tax law changes every Budget):
- Sec 115BBH: 30% on gains from transferring a VDA, plus 4% health and education cess. Surcharge is not modelled.
- Only the cost of acquisition is deducted; fees are not.
- Losses are reported but never set off against gains, and are not carried forward.
- Sec 194S: TDS deducted on transfers is credited against the tax.
"""
