"""Flask app: pages + JSON API for the Offline Payment Reconciliation Simulator."""
import math
import re
import sqlite3

from flask import Flask, jsonify, render_template, request

import database
import reconciliation
import simulator

app = Flask(__name__)
database.init_db()
ID_PATTERN = re.compile(r"^[A-Za-z0-9\-]{1,40}$")


def ok(message="OK", **data):
    return jsonify({"success": True, "message": message, **data})


def fail(message, code=400):
    return jsonify({"success": False, "message": message}), code


@app.errorhandler(sqlite3.Error)
def handle_db_error(e):
    app.logger.error("Database error: %s", e)          # details only in server log
    return fail("A database error occurred. Please try again.", 500)


@app.errorhandler(404)
def not_found(e):
    return fail("Not found", 404) if request.path.startswith("/api/") else (render_template("base.html", page="", error="Page not found"), 404)


@app.errorhandler(405)
def bad_method(e):
    return fail("Method not allowed", 405)


def tx_dict(row):
    """Row -> dict with delay information added."""
    d = dict(row)
    sec = reconciliation.delay_seconds(row)
    d["delay_seconds"] = sec
    d["delay_text"] = reconciliation.format_duration(sec)
    d["is_delayed_over_threshold"] = sec is not None and sec > database.DELAY_THRESHOLD_SECONDS
    return d


# ---------- pages ----------
@app.route("/")
def index():
    return render_template("index.html", page="dashboard")


@app.route("/transactions")
def transactions_page():
    return render_template("transactions.html", page="transactions")


@app.route("/audit")
def audit_page():
    return render_template("audit.html", page="audit")


# ---------- read API ----------
@app.route("/api/status")
def api_status():
    conn = database.get_db_connection()
    try:
        return ok(connectivity=database.get_connectivity(conn),
                  delay_threshold_seconds=database.DELAY_THRESHOLD_SECONDS)
    finally:
        conn.close()


@app.route("/api/transactions")
def api_transactions():
    status = (request.args.get("status") or "").upper()
    q = (request.args.get("q") or "").strip()[:50]
    limit = request.args.get("limit", type=int)
    if status and status != "ALL" and status not in database.STATUSES:
        return fail("Invalid status filter")
    sql, params = "SELECT * FROM transactions WHERE 1=1", []
    if status and status != "ALL":
        sql += " AND status = ?"
        params.append(status)
    if q:
        sql += " AND (transaction_id LIKE ? OR device_id LIKE ?)"
        params += ["%" + q + "%"] * 2
    sql += " ORDER BY id DESC"
    if limit and limit > 0:
        sql += " LIMIT ?"
        params.append(limit)
    conn = database.get_db_connection()
    try:
        rows = [tx_dict(r) for r in conn.execute(sql, params)]
        return ok(transactions=rows, count=len(rows))
    finally:
        conn.close()


@app.route("/api/transactions/<txn_id>")
def api_transaction(txn_id):
    if not ID_PATTERN.match(txn_id):
        return fail("Invalid transaction ID")
    conn = database.get_db_connection()
    try:
        row = conn.execute("SELECT * FROM transactions WHERE transaction_id = ?", (txn_id,)).fetchone()
        if not row:
            return fail("Transaction not found", 404)
        audit = [dict(a) for a in conn.execute(
            "SELECT * FROM audit_logs WHERE transaction_id = ? ORDER BY id", (txn_id,))]
        return ok(transaction=tx_dict(row), audit=audit,
                  verification=reconciliation.get_verification(conn, row))
    finally:
        conn.close()


@app.route("/api/statistics")
def api_statistics():
    conn = database.get_db_connection()
    try:
        return ok(statistics=database.get_statistics(conn))
    finally:
        conn.close()


@app.route("/api/audit")
def api_audit():
    txn_id = request.args.get("transaction_id")
    if txn_id and not ID_PATTERN.match(txn_id):
        return fail("Invalid transaction ID")
    conn = database.get_db_connection()
    try:
        if txn_id:
            rows = conn.execute("SELECT * FROM audit_logs WHERE transaction_id=? ORDER BY id DESC", (txn_id,))
        else:
            rows = conn.execute("SELECT * FROM audit_logs ORDER BY id DESC LIMIT 500")
        return ok(audit=[dict(r) for r in rows])
    finally:
        conn.close()


@app.route("/api/sync-progress")
def api_sync_progress():
    return ok(progress=simulator.SYNC_PROGRESS)


# ---------- action API ----------
def _set_connectivity(value):
    conn = database.get_db_connection()
    try:
        if database.get_connectivity(conn) == value:
            return fail("System is already %s" % value, 409)
        database.set_connectivity(conn, value)
        database.log_audit(conn, "SYSTEM", "CONNECTIVITY", "OFFLINE" if value == "ONLINE" else "ONLINE",
                           value, "Payment system went %s" % value)
        conn.commit()
        return ok("System is now %s" % value, connectivity=value)
    finally:
        conn.close()


@app.route("/api/offline", methods=["POST"])
def api_offline():
    return _set_connectivity("OFFLINE")


@app.route("/api/online", methods=["POST"])
def api_online():
    return _set_connectivity("ONLINE")


def _generate(kind, count):
    ids = []
    for _ in range(count):
        ids += simulator.generate_transaction(kind)
    return ids


def parse_amount(value, label):
    """Validate a money value: number between 1 and 100000, 2 decimals."""
    if isinstance(value, bool):
        raise ValueError("%s must be a number" % label)
    try:
        n = float(value)
    except (TypeError, ValueError):
        raise ValueError("%s must be a number" % label)
    if not math.isfinite(n) or not 1 <= n <= 100000:
        raise ValueError("%s must be between 1 and 100000" % label)
    return round(n, 2)


@app.route("/api/transactions/create", methods=["POST"])
def api_create():
    """Manually raise one transaction with user-supplied values."""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return fail("Invalid request body")
    kind = str(body.get("type", "NORMAL")).upper()
    device = str(body.get("device_id", ""))
    method = str(body.get("payment_method", "")).upper()
    if kind not in simulator.TYPES:
        return fail("Invalid scenario. Use one of: " + ", ".join(simulator.TYPES))
    if device not in simulator.DEVICES:
        return fail("Invalid device. Use one of: " + ", ".join(simulator.DEVICES))
    if method not in simulator.METHODS:
        return fail("Invalid payment method. Use one of: " + ", ".join(simulator.METHODS))
    try:
        amount = parse_amount(body.get("amount"), "Amount")
        delay, server_amount = None, None
        if kind == "DELAYED":
            delay = body.get("delay_minutes", 15)
            if isinstance(delay, bool) or not isinstance(delay, int) or not 1 <= delay <= 1440:
                return fail("Delay must be a whole number of minutes between 1 and 1440")
        if kind == "CONFLICT":
            if body.get("server_amount") in (None, ""):
                return fail("Server amount is required for a conflicting transaction")
            server_amount = parse_amount(body.get("server_amount"), "Server amount")
            if server_amount == amount:
                return fail("Server amount must differ from the offline amount to create a conflict")
    except ValueError as e:
        return fail(str(e))
    ids = simulator.generate_transaction(kind, device=device, amount=amount, method=method,
                                         delay_minutes=delay, server_amount=server_amount)
    conn = database.get_db_connection()
    try:
        offline = database.get_connectivity(conn) == "OFFLINE"
    finally:
        conn.close()
    where = "queued offline" if offline else "sent to the server"
    return ok("Raised %s (%s)" % (", ".join(ids), where), ids=ids)


@app.route("/api/transactions/generate", methods=["POST"])
def api_generate():
    body = request.get_json(silent=True) or {}
    kind = str(body.get("type", "NORMAL")).upper()
    if kind not in simulator.TYPES:
        return fail("Invalid transaction type. Use one of: " + ", ".join(simulator.TYPES))
    ids = _generate(kind, 1)
    return ok("Generated %s" % ", ".join(ids), ids=ids)


@app.route("/api/transactions/generate-batch", methods=["POST"])
def api_generate_batch():
    body = request.get_json(silent=True) or {}
    kind = str(body.get("type", "NORMAL")).upper()
    count = body.get("count", 10)
    if kind not in simulator.TYPES:
        return fail("Invalid transaction type")
    if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 100:
        return fail("Count must be a whole number between 1 and 100")
    ids = _generate(kind, count)
    return ok("Generated %d records" % len(ids), ids=ids)


@app.route("/api/synchronize", methods=["POST"])
def api_synchronize():
    conn = database.get_db_connection()
    try:
        if database.get_connectivity(conn) == "OFFLINE":
            return fail("System is offline. Restore the connection before synchronizing.", 409)
    finally:
        conn.close()
    if not simulator.SYNC_LOCK.acquire(blocking=False):  # ignore duplicate requests
        return fail("Synchronization already in progress", 409)
    try:
        result = simulator.synchronize_transactions(step_delay=0.08)
    finally:
        simulator.SYNC_LOCK.release()
    if result["processed"] == 0:
        conn = database.get_db_connection()
        try:   # explain WHY nothing was sent: they may already be synced and waiting for reconcile
            waiting = conn.execute("SELECT COUNT(*) FROM transactions "
                                   "WHERE status IN ('PENDING','SYNCING','SYNCED')").fetchone()[0]
        finally:
            conn.close()
        if waiting:
            return fail("Nothing left to synchronize. %d transaction(s) are already synced "
                        "and waiting - click Reconcile." % waiting)
        return fail("No pending transactions found")
    return ok("Synchronized %d, failed %d" % (result["synced"], result["failed"]), **result)


@app.route("/api/reconcile", methods=["POST"])
def api_reconcile():
    conn = database.get_db_connection()
    try:
        summary = reconciliation.reconcile_transactions(conn)
    finally:
        conn.close()
    if summary["processed"] == 0:
        return fail("No synchronized transactions to reconcile")
    return ok("Reconciled %d transactions" % summary["processed"], **summary)


@app.route("/api/reset", methods=["POST"])
def api_reset():
    conn = database.get_db_connection()
    try:
        database.reset_all(conn)
        conn.commit()
        return ok("Simulator reset")
    finally:
        conn.close()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
