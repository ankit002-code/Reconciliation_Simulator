# Offline Payment Reconciliation Simulator

## Project Overview
A shop POS terminal keeps taking payments when the internet drops. This simulator shows how those
**offline transactions** are later **synchronized, verified, matched and reconciled** with the online
server, while handling duplicates, conflicts, delays, failures and retries - with a full audit trail.

## Features
- Online/Offline toggle; offline payments are queued locally in SQLite
- **Raise a Transaction form**: enter device, amount, method and scenario yourself (delay minutes / server amount for those scenarios)
- Quick buttons to generate random normal, duplicate, delayed, conflicting and failed transactions
- Synchronization with live progress bar and automatic retries (attempts 1-2 fail, 3 succeeds)
- Duplicate / conflict / delay detection (configurable delay threshold, default 5 minutes)
- Reconciliation engine written in Python; every status change is written to `audit_logs`
- Dashboard cards, filterable transaction table, details modal, audit page, toasts, live activity log
- **Run Full Demo** button that plays the whole story automatically

## Technology Stack
Python 3, Flask, SQLite (`sqlite3`), HTML5, CSS3, Vanilla JavaScript (`fetch`). No other dependencies.

## Project Structure
```text
offline-payment-reconciliation/
├── app.py              Flask routes (pages + JSON API)
├── database.py         SQLite connection, tables, audit log, statistics
├── simulator.py        Transaction generator + synchronization
├── reconciliation.py   Verify / duplicate / conflict / delay checks + final status
├── requirements.txt    Flask
├── database/           reconciliation.db is created here automatically
├── templates/          base, index, transactions, audit
├── static/css/style.css
├── static/js/app.js
└── tests/test_reconciliation.py
```

## How It Works
```text
Customer -> Payment Device -> (online)  Server
                           -> (offline) LOCAL QUEUE -> Synchronize -> Verify -> Match -> Reconcile -> Final status
```
Status flow: `OFFLINE -> PENDING -> SYNCING -> SYNCED -> RECONCILED` (or `DUPLICATE`, `CONFLICT`,
`DELAYED`, `FAILED`). While the system is ONLINE, new transactions are sent to the server immediately.

## Database Design
- `transactions`: id, transaction_id, device_id, amount, payment_method, created_at, received_at, status,
  sync_status, transaction_type, retry_count, original_transaction_id, failure_reason, conflict_reason,
  plus `server_amount` (the amount the online server holds, used to detect conflicts)
- `audit_logs`: id, transaction_id, action, old_status, new_status, message, timestamp
- `system_state`: stores the ONLINE/OFFLINE flag

All SQL uses parameterized queries.

## Reconciliation Logic
For each `SYNCED` transaction, `reconcile_transaction()` checks in this order:
1. **Verify** data (ID format, amount range, known device and payment method) -> else `FAILED`
2. **Duplicate**: refers to an earlier `original_transaction_id`, or same device + amount + timestamp -> `DUPLICATE` (kept, never deleted)
3. **Conflict**: offline amount != server amount -> `CONFLICT` + `conflict_reason`
4. **Delay**: `received_at - created_at` > threshold -> `DELAYED`
5. Otherwise -> `RECONCILED`

Each result is written to the audit log. Change the threshold with the `DELAY_THRESHOLD_SECONDS` environment variable.

## Installation
```bash
python -m venv venv

# Windows
venv\Scripts\activate
# macOS / Linux
source venv/bin/activate

pip install -r requirements.txt
```

## Running the Application
```bash
python app.py
```
Open http://127.0.0.1:5000

## Demo Instructions
1. Click **Run Full Demo** and watch the activity log, progress bar and statistics.
2. Or do it by hand: **Go Offline** -> **Generate 10 Transactions** -> scenario buttons -> **Restore Connection** -> **Synchronize** (press it 3 times to clear failures) -> **Reconcile**.
3. Click any row to see details, verification checklist and audit history. Open **Audit Log** for the whole history.

## API Endpoints
| Method | URL | Purpose |
|---|---|---|
| GET | `/`, `/transactions`, `/audit` | Pages |
| GET | `/api/status` | Connectivity |
| GET | `/api/transactions?status=&q=&limit=` | List / filter |
| GET | `/api/transactions/<id>` | Details + audit + verification |
| GET | `/api/statistics` | Dashboard numbers |
| GET | `/api/audit` | Audit records |
| GET | `/api/sync-progress` | Sync progress |
| POST | `/api/offline`, `/api/online` | Change connectivity |
| POST | `/api/transactions/create` | Raise manually: `{"type","device_id","amount","payment_method","delay_minutes?","server_amount?"}` |
| POST | `/api/transactions/generate` | Body `{"type":"NORMAL\|DUPLICATE\|DELAYED\|CONFLICT\|FAILED"}` |
| POST | `/api/transactions/generate-batch` | Body `{"count":10,"type":"NORMAL"}` |
| POST | `/api/synchronize` | Sync offline + retry failed |
| POST | `/api/reconcile` | Run reconciliation |
| POST | `/api/reset` | Wipe everything |

## Example Scenarios
- **Duplicate**: `TXN-20261005-011` and `TXN-20261005-011-DUP` -> first reconciled, second `DUPLICATE`
- **Conflict**: offline Rs.159 vs server Rs.337 -> `CONFLICT`
- **Delayed**: created 38 min before it reached the server -> `DELAYED`
- **Failed**: sync fails twice (retry count 2), succeeds on third attempt -> `RECONCILED`

## Testing
```bash
python -m unittest discover tests
```
Tests use a temporary database, so your real data is safe.

## Future Improvements
Background auto-sync, per-device dashboards, CSV export, configurable failure rates, authentication,
and a real server API in place of the simulated one.
