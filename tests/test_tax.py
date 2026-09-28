import csv
import tempfile
import unittest
from datetime import date
from pathlib import Path

from trading_agent.tax.engine import compute, summarize
from trading_agent.tax.export import write_schedule_vda
from trading_agent.tax.importers import read_etherscan_csv, read_ledger_csv
from trading_agent.tax.models import Transaction, financial_year
from trading_agent.tax.store import LedgerStore


def tx(n, day, kind, **kw):
    return Transaction(uid=str(n), date=date.fromisoformat(day), type=kind, id=n, **kw)


class FinancialYearTests(unittest.TestCase):
    def test_april_to_march(self):
        self.assertEqual(financial_year(date(2026, 9, 28)), "2026-27")
        self.assertEqual(financial_year(date(2027, 3, 31)), "2026-27")
        self.assertEqual(financial_year(date(2026, 3, 31)), "2025-26")


class EngineTests(unittest.TestCase):
    def test_fifo_gain_uses_oldest_lot_first(self):
        result = compute([
            tx(1, "2026-04-10", "BUY", asset_in="ETH", qty_in=1, inr_value=100_000),
            tx(2, "2026-05-10", "BUY", asset_in="ETH", qty_in=1, inr_value=200_000),
            tx(3, "2026-06-10", "SELL", asset_out="ETH", qty_out=1.5, inr_value=450_000),
        ], {})
        self.assertEqual([(d.cost, d.consideration) for d in result.disposals], [(100_000, 300_000), (100_000, 150_000)])
        self.assertAlmostEqual(result.holdings["ETH"], 0.5)

    def test_losses_are_not_set_off(self):
        result = compute([
            tx(1, "2026-04-10", "BUY", asset_in="ETH", qty_in=1, inr_value=100_000),
            tx(2, "2026-04-10", "BUY", asset_in="SOL", qty_in=10, inr_value=100_000),
            tx(3, "2026-06-10", "SELL", asset_out="ETH", qty_out=1, inr_value=150_000, tds_inr=1_500),
            tx(4, "2026-06-10", "SELL", asset_out="SOL", qty_out=10, inr_value=40_000),
        ], {})
        s = summarize(result, "2026-27")
        self.assertEqual((s.gains, s.losses), (50_000, -60_000))
        self.assertEqual((s.tax, s.cess, s.total_tax), (15_000, 600, 15_600))
        self.assertEqual((s.tds_credit, s.balance_due), (1_500, 14_100))
        self.assertEqual([amount for _, amount in s.advance_tax], [2_115.0, 6_345.0, 10_575.0, 14_100.0])

    def test_swap_is_a_disposal_and_an_acquisition(self):
        result = compute([
            tx(1, "2026-04-10", "BUY", asset_in="ETH", qty_in=1, inr_value=100_000),
            tx(2, "2026-05-10", "SWAP", asset_out="ETH", qty_out=1, asset_in="USDC", qty_in=1_500, inr_value=120_000),
        ], {})
        self.assertEqual(result.disposals[0].gain, 20_000)
        self.assertEqual(result.holdings, {"USDC": 1_500})

    def test_price_table_fills_missing_values_and_fee_is_a_disposal(self):
        prices = {("ETH", date(2026, 4, 10)): 200_000.0, ("ETH", date(2026, 4, 11)): 250_000.0}
        result = compute([
            tx(1, "2026-04-10", "BUY", asset_in="ETH", qty_in=0.01),
            tx(2, "2026-04-11", "FEE", asset_out="ETH", qty_out=0.001),
        ], prices)
        self.assertEqual(result.review, [])
        self.assertAlmostEqual(result.disposals[0].cost, 200)
        self.assertAlmostEqual(result.disposals[0].consideration, 250)

    def test_review_flags_unclassified_unpriced_and_oversold(self):
        result = compute([
            tx(1, "2026-04-10", "UNCLASSIFIED", asset_in="ETH", qty_in=1),
            tx(2, "2026-04-11", "BUY", asset_in="ETH", qty_in=1),
            tx(3, "2026-04-12", "SELL", asset_out="SOL", qty_out=1, inr_value=5_000),
        ], {})
        self.assertEqual([i for i, _ in result.review], [1, 2, 3])
        self.assertEqual(result.disposals[0].cost, 0)

    def test_transfers_between_own_wallets_are_not_taxed(self):
        result = compute([
            tx(1, "2026-04-10", "BUY", asset_in="ETH", qty_in=1, inr_value=100_000),
            tx(2, "2026-04-11", "TRANSFER_OUT", asset_out="ETH", qty_out=1),
            tx(3, "2026-04-11", "TRANSFER_IN", asset_in="ETH", qty_in=1),
        ], {})
        self.assertEqual(result.disposals, [])
        self.assertEqual(result.holdings, {"ETH": 1})

    def test_income_is_reported_and_becomes_cost(self):
        result = compute([tx(1, "2026-04-10", "INCOME", asset_in="ETH", qty_in=0.1, inr_value=20_000),
                          tx(2, "2026-05-10", "SELL", asset_out="ETH", qty_out=0.1, inr_value=25_000)], {})
        self.assertEqual(summarize(result, "2026-27").other_income, 20_000)
        self.assertEqual(result.disposals[0].gain, 5_000)

    def test_no_advance_tax_under_threshold(self):
        result = compute([tx(1, "2026-04-10", "BUY", asset_in="ETH", qty_in=1, inr_value=100),
                          tx(2, "2026-04-11", "SELL", asset_out="ETH", qty_out=1, inr_value=200)], {})
        self.assertEqual(summarize(result, "2026-27").advance_tax, [])


class ImportExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_ledger_csv_round_trip_and_dedupe(self):
        path = self.dir / "trades.csv"
        path.write_text("date,type,asset_in,qty_in,asset_out,qty_out,inr_value,tds_inr,tx_ref,notes\n"
                        "2026-04-10,buy,eth,1,,,100000,,,first buy\n"
                        "2026-06-10,SELL,,,ETH,1,150000,1500,,\n", encoding="utf-8")
        store = LedgerStore(self.dir / "ledger.db")
        added = [store.add(t) for t in read_ledger_csv(path)] + [store.add(t) for t in read_ledger_csv(path)]
        self.assertEqual(added, [True, True, False, False])
        result = compute(store.all(), store.prices())
        out = self.dir / "vda.csv"
        self.assertEqual(write_schedule_vda(result, "2026-27", out), 1)
        with out.open(encoding="utf-8") as handle:
            rows = list(csv.reader(handle))
        self.assertEqual(rows[1][:6], ["2026-04-10", "2026-06-10", "Capital Gain", "100000.00", "150000.00", "50000.00"])
        store.close()

    def test_etherscan_rows_are_unclassified_and_fees_split_out(self):
        path = self.dir / "export.csv"
        path.write_text('"Transaction Hash","Blockno","UnixTimestamp","DateTime (UTC)","From","To","ContractAddress",'
                        '"Value_IN(ETH)","Value_OUT(ETH)","CurrentValue @ $1/Eth","TxnFee(ETH)","TxnFee(USD)",'
                        '"Historical $Price/Eth","Status","ErrCode","Method"\n'
                        '"0xaaa","1","1775000000","2026-04-01 00:00:00","0xex","0xme","","0.5","0","1","0","0","1","","",""\n'
                        '"0xbbb","2","1775100000","2026-04-02 03:46:40","0xme","0xshop","","0","0.1","1","0.0004","1","1","","","Transfer"\n',
                        encoding="utf-8")
        txs = read_etherscan_csv(path)
        self.assertEqual([(t.type, t.qty_in, t.qty_out) for t in txs],
                         [("UNCLASSIFIED", 0.5, 0), ("UNCLASSIFIED", 0, 0.1), ("FEE", 0, 0.0004)])


if __name__ == "__main__":
    unittest.main()
