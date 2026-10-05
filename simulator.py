"""Simulator: creates transactions (normal/duplicate/delayed/conflict/failed) and synchronizes them."""
import random
import threading
import time
from datetime import datetime, timedelta

import database
from database import get_db_connection, log_audit, now_str, TS_FORMAT

DEVICES = ["POS-01", "POS-02", "POS-03", "POS-04"]
METHODS = ["UPI", "CARD", "IB", "WALLET"]
TYPES = ["NORMAL", "DUPLICATE", "DELAYED", "CONFLICT", "FAILED"]
FAILED_ATTEMPTS_BEFORE_SUCCESS = 2   # attempt 1 & 2 fail, attempt 3 succeeds

# Live progress shown by the dashboard progress bar
SYNC_PROGRESS = {"running": False, "total": 0, "done": 0}
SYNC_LOCK = threading.Lock()


def next_transaction_id(conn):
    """TXN-YYYYMMDD-001 style IDs (count only grows, so IDs stay unique)."""
    prefix = "TXN-" + datetime.now().strftime("%Y%m%d")
    n = conn.execute("SELECT COUNT(*) FROM transactions WHERE transaction_id LIKE ?",
                     (prefix + "-%",)).fetchone()[0]
    return "%s-%03d" % (prefix, n + 1)


def _insert(conn, txn_id, device, amount, method, created_at, ttype,
            original=None, server_amount=None):
    conn.execute(
        "INSERT INTO transactions (transaction_id, device_id, amount, payment_method, created_at, "
        "status, sync_status, transaction_type, original_transaction_id, server_amount) "
        "VALUES (?, ?, ?, ?, ?, 'OFFLINE', 'NOT_SYNCED', ?, ?, ?)",
        (txn_id, device, amount, method, created_at, ttype, original, server_amount))
    log_audit(conn, txn_id, "CREATED", None, "OFFLINE",
              "Transaction stored in local offline queue (%s)" % ttype)


def generate_transaction(kind="NORMAL", device=None, amount=None, method=None,
                         delay_minutes=None, server_amount=None):
    """Create one transaction (two for DUPLICATE). Values left as None are random;
    pass device/amount/method/delay_minutes/server_amount to raise one manually.
    Returns list of transaction IDs.
    If the system is ONLINE the new records are submitted to the server immediately."""
    if kind not in TYPES:
        raise ValueError("Invalid transaction type")
    conn = get_db_connection()
    try:
        online = database.get_connectivity(conn) == "ONLINE"
        txn_id = next_transaction_id(conn)
        device = device or random.choice(DEVICES)
        method = method or random.choice(METHODS)
        amount = float(amount) if amount is not None else float(random.randint(50, 5000))
        created = datetime.now()
        if kind == "DELAYED":                      # old timestamp = artificial delay
            created -= timedelta(minutes=delay_minutes or random.randint(10, 45))
        if kind == "CONFLICT" and server_amount is None:   # server has a different amount
            server_amount = max(amount + random.choice([-1, 1]) * random.randint(20, 400), 1.0)
            if server_amount == amount:
                server_amount += 100
        _insert(conn, txn_id, device, amount, method, created.strftime(TS_FORMAT), kind,
                server_amount=server_amount if kind == "CONFLICT" else None)
        ids = [txn_id]
        if kind == "DUPLICATE":                    # second record pointing to the first one
            dup_id = txn_id + "-DUP"
            _insert(conn, dup_id, device, amount, method, created.strftime(TS_FORMAT),
                    "DUPLICATE", original=txn_id)
            ids.append(dup_id)
        conn.commit()
    finally:
        conn.close()
    if online:                                     # online payments are sent straight away
        synchronize_transactions(ids=ids)
    return ids


def _sync_one(conn, tx):
    """Move one transaction OFFLINE -> PENDING -> SYNCING -> SYNCED (or FAILED)."""
    tid, old = tx["transaction_id"], tx["status"]
    retry = tx["retry_count"]
    msg = "Transaction queued for synchronization" if old == "OFFLINE" else \
        "Retry #%d queued for synchronization" % retry
    conn.execute("UPDATE transactions SET status='PENDING', sync_status='NOT_SYNCED' WHERE id=?", (tx["id"],))
    log_audit(conn, tid, "QUEUED", old, "PENDING", msg)
    conn.execute("UPDATE transactions SET status='SYNCING', sync_status='SYNCING' WHERE id=?", (tx["id"],))
    log_audit(conn, tid, "SYNC_START", "PENDING", "SYNCING", "Sending transaction to server")
    # Simulated failure: FAILED-type transactions fail their first attempts
    if tx["transaction_type"] == "FAILED" and retry < FAILED_ATTEMPTS_BEFORE_SUCCESS:
        reason = "Server timeout during synchronization (attempt %d)" % (retry + 1)
        conn.execute("UPDATE transactions SET status='FAILED', sync_status='FAILED', retry_count=?, "
                     "failure_reason=? WHERE id=?", (retry + 1, reason, tx["id"]))
        log_audit(conn, tid, "SYNC_FAILED", "SYNCING", "FAILED", reason)
        return "failed"
    conn.execute("UPDATE transactions SET status='SYNCED', sync_status='SYNCED', received_at=?, "
                 "failure_reason=NULL WHERE id=?", (now_str(), tx["id"]))
    log_audit(conn, tid, "SYNC_OK", "SYNCING", "SYNCED", "Transaction synchronized successfully")
    return "synced"


def synchronize_transactions(ids=None, step_delay=0.0):
    """Sync every OFFLINE transaction and retry every FAILED one.
    step_delay slows things down so the progress bar is visible."""
    conn = get_db_connection()
    try:
        rows = conn.execute("SELECT * FROM transactions WHERE status IN ('OFFLINE','FAILED') ORDER BY id").fetchall()
        if ids is not None:
            rows = [r for r in rows if r["transaction_id"] in ids]
        SYNC_PROGRESS.update(running=True, total=len(rows), done=0)
        result = {"processed": 0, "synced": 0, "failed": 0, "synced_ids": [], "failed_ids": []}
        for tx in rows:
            outcome = _sync_one(conn, tx)
            conn.commit()
            result["processed"] += 1
            result[outcome] += 1
            result[outcome + "_ids"].append(tx["transaction_id"])
            SYNC_PROGRESS["done"] += 1
            if step_delay:
                time.sleep(step_delay)
        return result
    finally:
        SYNC_PROGRESS["running"] = False
        conn.close()
