"""Reconciliation engine: verify, detect duplicates/conflicts/delays, assign final status."""
from datetime import datetime

import database
from database import log_audit, TS_FORMAT
from simulator import DEVICES, METHODS


def delay_seconds(tx):
    """Seconds between created_at and received_at (None if not received yet)."""
    if not tx["received_at"]:
        return None
    created = datetime.strptime(tx["created_at"], TS_FORMAT)
    received = datetime.strptime(tx["received_at"], TS_FORMAT)
    return int((received - created).total_seconds())


def format_duration(seconds):
    if seconds is None:
        return "-"
    m, s = divmod(max(seconds, 0), 60)
    h, m = divmod(m, 60)
    return "%dh %dm %ds" % (h, m, s) if h else "%dm %ds" % (m, s)


def verify_transactions(tx):
    """Basic data checks. Returns list of (label, passed)."""
    return [
        ("Transaction ID valid", bool(tx["transaction_id"]) and tx["transaction_id"].startswith("TXN-")),
        ("Amount verified", tx["amount"] is not None and 0 < tx["amount"] <= 100000),
        ("Device verified", tx["device_id"] in DEVICES),
        ("Payment method verified", tx["payment_method"] in METHODS),
    ]


def detect_duplicates(conn, tx):
    """Return the ID of the original transaction if `tx` is a duplicate, else None."""
    orig = tx["original_transaction_id"]
    if orig:  # explicit reference to an earlier record
        if conn.execute("SELECT 1 FROM transactions WHERE transaction_id=?", (orig,)).fetchone():
            return orig
    # Same device + amount + timestamp as an earlier record
    row = conn.execute(
        "SELECT transaction_id FROM transactions WHERE id < ? AND device_id=? AND amount=? "
        "AND created_at=? AND transaction_id != ? LIMIT 1",
        (tx["id"], tx["device_id"], tx["amount"], tx["created_at"], tx["transaction_id"])).fetchone()
    return row["transaction_id"] if row else None


def detect_conflicts(tx):
    """Return a reason string if the offline and online amounts differ, else None."""
    if tx["server_amount"] is not None and tx["server_amount"] != tx["amount"]:
        return "Amount mismatch between offline and online record (offline Rs.%g vs online Rs.%g)" % (
            tx["amount"], tx["server_amount"])
    return None


def detect_delays(tx, threshold=None):
    """Return delay in seconds if it exceeds the threshold, else None."""
    threshold = database.DELAY_THRESHOLD_SECONDS if threshold is None else threshold
    d = delay_seconds(tx)
    return d if d is not None and d > threshold else None


def get_verification(conn, tx):
    """Checklist shown in the details modal."""
    checks = verify_transactions(tx)
    checks.append(("No duplicate found", detect_duplicates(conn, tx) is None))
    checks.append(("No conflict found", detect_conflicts(tx) is None))
    checks.append(("Delivered on time", detect_delays(tx) is None))
    checks.append(("Synchronization succeeded", tx["sync_status"] == "SYNCED"))
    return [{"label": l, "ok": bool(ok)} for l, ok in checks]


def reconcile_transaction(conn, tx):
    """Run all checks on one SYNCED transaction and assign the final status."""
    tid, old = tx["transaction_id"], tx["status"]
    failed = [l for l, ok in verify_transactions(tx) if not ok]
    dup = detect_duplicates(conn, tx)
    conflict = detect_conflicts(tx)
    delay = detect_delays(tx)
    if tx["sync_status"] != "SYNCED":
        new, msg = "FAILED", "Synchronization result missing"
    elif failed:
        new, msg = "FAILED", "Verification failed: " + ", ".join(failed)
        conn.execute("UPDATE transactions SET failure_reason=? WHERE id=?", (msg, tx["id"]))
    elif dup:
        new, msg = "DUPLICATE", "Duplicate transaction detected (original: %s). Kept for audit." % dup
    elif conflict:
        new, msg = "CONFLICT", conflict
        conn.execute("UPDATE transactions SET conflict_reason=? WHERE id=?", (conflict, tx["id"]))
    elif delay:
        new, msg = "DELAYED", "Delayed by %s (threshold %s)" % (
            format_duration(delay), format_duration(database.DELAY_THRESHOLD_SECONDS))
    else:
        new, msg = "RECONCILED", "Transaction verified and reconciled"
    conn.execute("UPDATE transactions SET status=? WHERE id=?", (new, tx["id"]))
    log_audit(conn, tid, "RECONCILE", old, new, msg)
    return new


def reconcile_transactions(conn):
    """Reconcile every SYNCED transaction. Returns a summary dict."""
    rows = conn.execute("SELECT * FROM transactions WHERE status='SYNCED' ORDER BY id").fetchall()
    summary = {"processed": 0, "reconciled": 0, "duplicates": 0, "conflicts": 0,
               "delayed": 0, "failed": 0}
    keys = {"RECONCILED": "reconciled", "DUPLICATE": "duplicates", "CONFLICT": "conflicts",
            "DELAYED": "delayed", "FAILED": "failed"}
    for tx in rows:
        summary[keys[reconcile_transaction(conn, tx)]] += 1
        summary["processed"] += 1
    conn.commit()
    return summary
