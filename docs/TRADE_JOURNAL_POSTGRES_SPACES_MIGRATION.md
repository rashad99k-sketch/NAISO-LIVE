# Trade Journal: Managed PostgreSQL + Spaces Migration Plan

## Status and safety boundary

Planning only. No production deployment, LIVE exchange calls, database mutation, Managed Database, or Spaces bucket has been created by this change.

The inspected App Platform runtime database at `/workspace/data/ggp_trade_journal.sqlite3` had valid SQLite structure but zero rows in `trades` and `trade_events`. This proves only that the current container's database is empty; it does not establish when or why prior rows disappeared.

Keep the current BingX exposure under `RECOVERY_HOLD` until symbol, side, open quantity, actual entry, original native stop, and management state are verified from authoritative exchange records. Never invent TP/SL or trade state.

## Target architecture

- Managed PostgreSQL is the authoritative durable Trade Journal.
- A private DigitalOcean Spaces bucket stores encrypted, timestamped backups and migration manifests. Spaces is object storage, not a live SQL database.
- App Platform connects using a secret connection URI (normally `DATABASE_URL`) with TLS. Restrict database trusted sources to the app and approved operators; prefer private networking when region/VPC placement supports it.
- Keep strategy, dashboard, scanner, sizing, leverage, execution semantics, Native SL policy, TP1/TP2, and recovery state-machine behavior unchanged.

Official DigitalOcean references:
- App Platform database attachment and Managed Database guidance: https://docs.digitalocean.com/products/app-platform/how-to/manage-databases/
- PostgreSQL TLS and trusted sources: https://docs.digitalocean.com/products/databases/postgresql/how-to/secure/
- Spaces S3 compatibility: https://docs.digitalocean.com/products/spaces/reference/s3-compatibility/
- Spaces bucket versioning: https://docs.digitalocean.com/products/spaces/how-to/enable-versioning/
- App Platform VPC connectivity: https://docs.digitalocean.com/products/app-platform/how-to/enable-vpc/

## Existing TradeJournal contract

Preserve the current method names, accepted arguments, return semantics, column meanings, and transaction guarantees:
- `persist_entry_pending(...)`: commits entry identity and planned management state before any exchange create_order call.
- `record_execution(...)`: records exchange order/fill evidence.
- `start_trade(...)`: idempotent trade start/upsert.
- `update(trade_id, **fields)`: lifecycle and management fields.
- `save_snapshot(trade_id, state, event_key, event_type)`: snapshot and event checkpoint in one transaction.
- `event(trade_id, event_key, event_type, payload)`: idempotent insert; unique (trade_id,event_key).
- `finalize_once(...)`: one-time finalization.
- `is_event_consumed(event_id)` / `consume_event(event_id, trade_id)`: restart-safe event consumption.
- `get(trade_id)` / `open_trades()`: recovery reads.

## PostgreSQL implementation

1. Define a storage protocol for the above contract; keep current callers backend-agnostic.
2. Implement a separate PostgreSQL adapter. Do not remove SQLite until PostgreSQL parity tests pass.
3. Use Psycopg 3, parameterized SQL, explicit transactions, bounded connect/statement timeouts, and deterministic connection cleanup.
4. Version schema migrations. Preserve all existing fields. Suggested types: BIGINT identity for event IDs, BOOLEAN for flags, JSONB for state/payload, and numeric/timestamp mappings only after exact round-trip tests against current SQLite values.
5. Preserve constraints: trades.trade_id primary key; partial unique client_order_id; unique trade_events(trade_id,event_key); consumed_events.event_id primary key.
6. Preserve atomic boundaries: pre-entry intent before exchange order; execution evidence + event; snapshot + event; finalization + event.
7. Database unavailable/write failure must fail closed for new LIVE entries. Do not make an exchange order retry because a DB retry occurred. Use stable trade_id/client_order_id/event_key for idempotent DB retries.
8. Startup readiness must verify connection, schema version, and a write/read/delete-or-rollback health probe before enabling entries. Never expose credentials in logs.

## Phased migration

### Phase 0 — protect exposure and gather evidence
- Keep Recovery Hold. Do not restart/deploy as a diagnostic shortcut.
- Read-only verify BingX position identity, quantity, entry, original native SL order/trigger/quantity, and order/trade history.
- Capture runtime logs around original entry and first journal_row_not_found.
- Search only approved backups/artifacts for an older SQLite journal. Do not create a fake historical row.

### Phase 1 — provision infrastructure (operator action)
- Create Managed PostgreSQL in a region/network reachable by the FRA1 App Platform service; confirm the region-to-datacenter/VPC mapping before selecting private connectivity.
- Attach it to the App Platform app and restrict trusted sources to that app.
- Create a private Spaces bucket and least-privilege key. Enable bucket versioning using the Spaces API and verify the setting.
- Do not point production traffic to the new database yet.

### Phase 2 — implement adapter and tests
- Add the PostgreSQL adapter and schema migration runner separately from SQLite.
- Run the same contract tests against SQLite and a disposable PostgreSQL database.
- Add fault-injection for DB disconnect, timeout, transaction rollback, duplicate replay, concurrent writers, and DB unavailable before order submission.
- No production DB credentials in CI.

### Phase 3 — rehearsal
- Create a consistent SQLite backup with SQLite's backup API; do not raw-copy a live WAL database.
- Record SHA-256, file size, schema version, table counts, and integrity result.
- The currently inspected SQLite file is empty; do not mistake it for the missing historical database.
- Import available source data into staging PostgreSQL; compare trade IDs, event keys, consumed IDs, JSON state, NULLs, timestamps, and numeric values.
- Back up staging PostgreSQL to encrypted Spaces object; download, verify checksum, restore into a disposable database, and rerun tests.

### Phase 4 — shadow validation
- Run a non-LIVE process against PostgreSQL with deterministic fixtures.
- Validate startup recovery, OPEN/FLAT/UNKNOWN, missing/multiple/finalized journal row vs OPEN exchange exposure, partial close, TP1/TP2, native SL missing/mismatch, and restart cases.
- Confirm no strategy, sizing, leverage, execution, dashboard, or scanner behavior changes.

### Phase 5 — controlled production cutover (separate explicit approval required)
- Stop new entries through the existing safe procedure; do not disturb an open exchange position.
- Take final consistent SQLite backup and manifest.
- Apply schema migrations, import verified records, and reconcile counts/checksums.
- Change only journal backend configuration; retain a read-only SQLite backup.
- Start in guarded mode. Verify DB connectivity, schema version, journal read/write/read-back, exchange tri-state, and Native SL read-back.
- Enable LIVE entries only after all gates pass and the operator explicitly approves.

## Configuration names (values must be set in DigitalOcean secrets)

- `DATABASE_URL` (secret)
- `DB_CONNECT_TIMEOUT_SEC`
- `DB_STATEMENT_TIMEOUT_MS`
- `DO_SPACES_ACCESS_KEY_ID` (secret)
- `DO_SPACES_SECRET_ACCESS_KEY` (secret)
- `DO_SPACES_BUCKET`
- `DO_SPACES_REGION` / `DO_SPACES_ENDPOINT`
- `BACKUP_ENCRYPTION_KEY` (secret, stored outside repository and Spaces)

## Backup policy

- Daily full backup and mandatory pre-migration/pre-release backup.
- Unique UTC timestamp, schema version, and SHA-256 manifest per backup.
- Client-side encryption before upload; private ACL; bucket versioning; verify uploaded and downloaded checksums.
- Scheduled restore drill into a disposable database. A backup is not accepted until restore and journal contract tests pass.
- Alert on backup age, failure, checksum mismatch, and restore-test failure.

## Compatibility test matrix / release gates

Run the same suite against SQLite and PostgreSQL:
1. Empty schema creation and forward-only migration.
2. Pre-entry pending row/event commits before exchange MARKET order invocation.
3. Missing journal or write exception blocks LIVE order; zero MARKET calls.
4. Fill evidence persists actual filled quantity, price, order ID, and event atomically.
5. Duplicate event_key is idempotent.
6. Snapshot and event commit together; injected failure rolls both back.
7. consume_event remains idempotent across separate connections/restarts.
8. get/open_trades preserve JSON and NULL semantics.
9. finalize_once is one-shot under concurrent callers.
10. Restart preserves open rows, TP1/TP2 flags, trailing state, Native SL evidence, and event history.
11. DB timeout/disconnect returns explicit persistence failure and blocks new LIVE entries.
12. Concurrent writers preserve unique constraints and transaction isolation.
13. OPEN/FLAT/UNKNOWN recovery: UNKNOWN is never FLAT; no SL/TP fabrication.
14. Backup/restore reproduces counts, unique keys, checksums, and readable management state.

No merge to main, App Platform deployment, production DB cutover, or LIVE enablement until all unit, contract, integration, recovery, fault-injection, and restore tests pass; secrets are checked; current BingX exposure is safely resolved or fully evidenced; and the user separately approves production cutover.

## Known limitations

- Current runtime SQLite contains zero trades and zero trade events; this does not prove no older DB or backup exists elsewhere.
- PR #1 is SQLite path/fail-closed hardening only; it is not the PostgreSQL migration. It has not been merged, deployed, or test-run.
- This branch provisions no cloud resources and contains no production credentials.
