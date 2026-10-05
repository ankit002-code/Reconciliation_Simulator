"""Run with:  python -m unittest discover tests"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["RECON_DB_PATH"] = _tmp.name        # tests never touch the real database

import database          # noqa: E402
import reconciliation    # noqa: E402
import simulator         # noqa: E402


def one(conn, txn_id):
    return conn.execute("SELECT * FROM transactions WHERE transaction_id=?", (txn_id,)).fetchone()


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        database.init_db()
        self.conn = database.get_db_connection()
        database.reset_all(self.conn)
        database.set_connectivity(self.conn, "OFFLINE")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def full_cycle(self, kind, syncs=1):
        ids = simulator.generate_transaction(kind)
        for _ in range(syncs):
            simulator.synchronize_transactions()
        reconciliation.reconcile_transactions(self.conn)
        return ids

    def test_normal_transaction_reconciles(self):
        (tid,) = self.full_cycle("NORMAL")
        tx = one(self.conn, tid)
        self.assertEqual(tx["status"], "RECONCILED")
        self.assertEqual(tx["sync_status"], "SYNCED")

    def test_duplicate_detected_and_kept(self):
        orig, dup = self.full_cycle("DUPLICATE")
        self.assertEqual(one(self.conn, orig)["status"], "RECONCILED")
        self.assertEqual(one(self.conn, dup)["status"], "DUPLICATE")   # not deleted

    def test_conflict_detected(self):
        (tid,) = self.full_cycle("CONFLICT")
        tx = one(self.conn, tid)
        self.assertEqual(tx["status"], "CONFLICT")
        self.assertIn("Amount mismatch", tx["conflict_reason"])

    def test_delay_detected(self):
        (tid,) = self.full_cycle("DELAYED")
        tx = one(self.conn, tid)
        self.assertEqual(tx["status"], "DELAYED")
        self.assertGreater(reconciliation.delay_seconds(tx), database.DELAY_THRESHOLD_SECONDS)

    def test_failed_transaction_retries_then_succeeds(self):
        (tid,) = simulator.generate_transaction("FAILED")
        for expected_retry in (1, 2):                    # attempts 1 and 2 fail
            simulator.synchronize_transactions()
            tx = one(self.conn, tid)
            self.assertEqual((tx["status"], tx["retry_count"]), ("FAILED", expected_retry))
            self.assertTrue(tx["failure_reason"])
        simulator.synchronize_transactions()             # attempt 3 succeeds
        self.assertEqual(one(self.conn, tid)["status"], "SYNCED")
        reconciliation.reconcile_transactions(self.conn)
        self.assertEqual(one(self.conn, tid)["status"], "RECONCILED")

    def test_offline_transactions_start_offline(self):
        (tid,) = simulator.generate_transaction("NORMAL")
        tx = one(self.conn, tid)
        self.assertEqual((tx["status"], tx["sync_status"]), ("OFFLINE", "NOT_SYNCED"))

    def test_audit_log_written(self):
        (tid,) = self.full_cycle("NORMAL")
        trail = [r["new_status"] for r in self.conn.execute(
            "SELECT new_status FROM audit_logs WHERE transaction_id=? ORDER BY id", (tid,))]
        self.assertEqual(trail, ["OFFLINE", "PENDING", "SYNCING", "SYNCED", "RECONCILED"])

    def test_reset(self):
        self.full_cycle("DUPLICATE")
        database.reset_all(self.conn)
        self.conn.commit()
        self.assertEqual(database.get_statistics(self.conn)["total"], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0], 0)
        self.assertEqual(database.get_connectivity(self.conn), "ONLINE")


class ManualRaiseTests(unittest.TestCase):
    """Manually raised transactions (service + API)."""
    def setUp(self):
        database.init_db()
        self.conn = database.get_db_connection()
        database.reset_all(self.conn)
        database.set_connectivity(self.conn, "OFFLINE")
        self.conn.commit()
        import app as flask_app
        self.client = flask_app.app.test_client()

    def tearDown(self):
        self.conn.close()

    def test_manual_conflict_uses_given_values(self):
        (tid,) = simulator.generate_transaction("CONFLICT", device="POS-02", amount=500,
                                                method="UPI", server_amount=700)
        simulator.synchronize_transactions()
        reconciliation.reconcile_transactions(self.conn)
        tx = one(self.conn, tid)
        self.assertEqual((tx["device_id"], tx["amount"], tx["payment_method"]), ("POS-02", 500.0, "UPI"))
        self.assertEqual(tx["status"], "CONFLICT")
        self.assertIn("700", tx["conflict_reason"])

    def test_manual_delay_minutes(self):
        (tid,) = simulator.generate_transaction("DELAYED", amount=99, delay_minutes=20)
        simulator.synchronize_transactions()
        reconciliation.reconcile_transactions(self.conn)
        tx = one(self.conn, tid)
        self.assertEqual(tx["status"], "DELAYED")
        self.assertTrue(1195 <= reconciliation.delay_seconds(tx) <= 1210)

    def test_api_create_valid(self):
        r = self.client.post("/api/transactions/create", json={
            "type": "NORMAL", "device_id": "POS-03", "payment_method": "card", "amount": "450.50"})
        self.assertTrue(r.get_json()["success"])
        tx = one(self.conn, r.get_json()["ids"][0])
        self.assertEqual((tx["amount"], tx["payment_method"], tx["status"]), (450.5, "CARD", "OFFLINE"))

    def test_api_create_rejects_bad_input(self):
        bad = [
            {"type": "NORMAL", "device_id": "POS-03", "payment_method": "UPI", "amount": "abc"},
            {"type": "NORMAL", "device_id": "POS-03", "payment_method": "UPI", "amount": -5},
            {"type": "NORMAL", "device_id": "POS-99", "payment_method": "UPI", "amount": 10},
            {"type": "NORMAL", "device_id": "POS-01", "payment_method": "BITCOIN", "amount": 10},
            {"type": "CONFLICT", "device_id": "POS-01", "payment_method": "UPI", "amount": 10},
            {"type": "CONFLICT", "device_id": "POS-01", "payment_method": "UPI", "amount": 10, "server_amount": 10},
        ]
        for body in bad:
            r = self.client.post("/api/transactions/create", json=body)
            self.assertEqual(r.status_code, 400, body)
            self.assertFalse(r.get_json()["success"])
        self.assertEqual(database.get_statistics(self.conn)["total"], 0)


if __name__ == "__main__":
    unittest.main()
