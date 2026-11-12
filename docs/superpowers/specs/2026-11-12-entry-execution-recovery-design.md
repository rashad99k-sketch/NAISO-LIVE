# Entry Execution Recovery Design

## Intent
Close two production-safety gaps in the live entry lifecycle: a stale _ACTIVE_TRADE lock after a confirmed flat exchange state, and partial/ambiguous market-order outcomes that are not durably represented or protected.

## Goals
- Release the entry lock only after the entry order is terminal, zero-fill is confirmed, and BingX confirms no open position.
- For a partial market fill, cancel the unfilled remainder, read back the terminal order and actual position, then protect the confirmed exposure with the existing native STOP_MARKET gateway.
- Keep all unresolved states fail-closed: preserve the lock, set recovery blocking, surface the exact reason, and never retry a non-idempotent market order.
- Preserve accurate execution state and pending-entry lifecycle in the SQLite journal, including rejected entries with verified zero exposure.
- Stop candidate iteration immediately when execution becomes unresolved or exposure is detected.

## Non-goals and constraints
- No changes to institutional signal generation, RORO/Event ID policy, scanner ranking, entry evidence, SL/TP calculation, trade sizing, leverage policy, or profit management.
- Do not weaken the structural stop requirement, exchange read-back, native protection matching, or one-market-order/no-blind-retry policy.
- Do not infer FLAT from an unavailable/invalid position response.
- Never represent a partial or unknown fill as a completed fill.

## Approaches considered
1. Clear _ACTIVE_TRADE whenever positions report FLAT. Rejected: it can unlock while the market order is still active or its outcome remains unknown.
2. Release the lock for every non-FILLED execution result. Rejected: partial fills and request timeouts can leave live exposure and permit a duplicate entry.
3. Track the entry attempt through exchange terminal status, position reconciliation, protection, and journal finalization. Chosen: it is the only approach that preserves the existing single execution gateway and prevents both ghost locks and duplicate/unprotected exposure.

## Implementation decisions
- TradeExecutionService will cancel an outstanding remainder after a partial fill and require terminal-order read-back plus a matching exchange position before declaring that partial exposure safe to adopt.
- A confirmed partial exposure is treated as a real position at its actual filled quantity and goes through the existing native stop placement/read-back path.
- If cancellation, order read-back, position matching, average price, or stop verification remains ambiguous, the bot enters RECOVERY_HOLD, blocks all new entries, and retains the execution lock.
- A zero-fill rejected/canceled entry may be finalized separately from a trade close, but only after an exchange-flat confirmation.
- Startup recovery must not misclassify an unresolved ENTRY_PENDING row as an externally closed trade.

## Validation
Add deterministic exchange-mock regression tests for partial-fill cancel/read-back, cancellation ambiguity, terminal zero-fill release, unknown-order retention, and recovery blocking. Run targeted tests and the full pytest suite in GitHub Actions before considering the branch ready.
