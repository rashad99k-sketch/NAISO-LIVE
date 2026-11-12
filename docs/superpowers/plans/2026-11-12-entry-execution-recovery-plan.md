# Entry Execution Recovery Lifecycle Implementation Plan

> For agentic workers: use the executing-plans workflow task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Make live entry attempts recoverable and fail-closed across rejected, partial, uncertain, and protected execution outcomes.

**Architecture:** Keep TradeExecutionService as the only market-entry gateway. It must reconcile partial orders and confirm the resulting exchange position before GGP adopts exposure; GGP then owns lock/recovery state, while SQLite records terminal zero-exposure rejections distinctly from closed trades.

**Tech Stack:** Python 3.11, CCXT/BingX, pytest, SQLite.

**Spec:** docs/superpowers/specs/2026-11-12-entry-execution-recovery-design.md

## Global Constraints
- Do not change strategy signals, entry evidence, scanner ranking, Event ID consumption policy, SL/TP calculations, position sizing, leverage, or profit management.
- Do not retry a non-idempotent MARKET order.
- Do not clear recovery blocking or release the entry lock on FLAT alone when an entry outcome is pending.
- Do not mark a partial or unknown fill as fully filled.
- A LIVE position must not be treated as protected until BingX native STOP_MARKET read-back verifies the required symbol, side, quantity, trigger, and active status.

## Review Focus
- A partial order whose cancel request succeeds but terminal read-back fails must remain blocked and must not be adopted as a fully reconciled position.
- A full fill with missing average price must remain recoverable and protected where the exchange position supplies a trustworthy entry price; otherwise it must remain held.
- A market-order timeout with no fetchable order must never unlock on a FLAT position snapshot alone.
- A terminal canceled/rejected zero-fill entry must not be recorded as an externally closed trade on restart.
- A STOP_MARKET creation/read-back exception after a confirmed fill must preserve the exchange exposure and prevent new entries.

---

### Task 1: Reconcile partial market fills in the execution gateway

**Files:**
- Modify: bingx_execution.py
- Test: test_bingx_execution.py

**Interfaces:**
- Produces: ExecutionResult.order_terminal: bool; TradeExecutionService.submit_market_entry(...) returns PARTIAL_FILL only after the remainder is terminal and the matching live position is read back. Ambiguous cancel/read-back remains EXECUTION_UNCERTAIN.

- [ ] Step 1: Add a failing partial-fill cancellation test.
  - Extend the exchange fake with cancel_order and a mutable post-cancel order/position.
  - Test: test_partial_fill_cancels_remainder_and_verifies_actual_position.
  - Assert one MARKET submission, terminal canceled remainder, actual filled quantity, matching position, and order_terminal is True.

- [ ] Step 2: Run the focused test to verify RED.
  - Run: pytest -q test_bingx_execution.py::test_partial_fill_cancels_remainder_and_verifies_actual_position
  - Expected: FAIL because the execution result does not reconcile/cancel the outstanding remainder.

- [ ] Step 3: Add failing tests for ambiguous cancellation and pre-submission rejection.
  - Tests: test_partial_fill_cancel_without_terminal_readback_is_uncertain and test_unknown_position_mode_is_rejected_before_market_submission.
  - Assert ambiguity is not considered safe, and a position-mode failure before create_order is not mislabeled as a submitted order.

- [ ] Step 4: Implement minimal gateway changes.
  - Add order_terminal to ExecutionResult.
  - For partial fills with remaining quantity, cancel only the existing remainder (never submit another MARKET order), fetch the order again, and require a terminal state.
  - Re-fetch the expected position side and require its quantity to match the final executed quantity before returning a reconciled PARTIAL_FILL.
  - Use a trustworthy exchange position entry price as a fallback when order average and trade-average data are missing.
  - Return EXECUTION_UNCERTAIN whenever cancellation, order status, position side/quantity, or fill evidence cannot be reconciled.

- [ ] Step 5: Run the focused gateway tests.
  - Run: pytest -q test_bingx_execution.py
  - Expected: all tests pass; MARKET create_order is called once per attempt.

- [ ] Step 6: Commit Task 1.
  - Commit message: fix: reconcile partial BingX market fills safely

### Task 2: Make GGP lock, recovery, and journal lifecycle consistent

**Files:**
- Modify: ggpbot.py
- Modify: roro_trade_journal.py
- Test: test_recovery_lifecycle.py

**Interfaces:**
- Consumes: ExecutionResult.order_terminal from Task 1.
- Produces: a pending-entry reconciliation path that releases _ACTIVE_TRADE only after terminal zero-fill proof plus exchange FLAT; confirmed partial exposure is represented and protected through the existing GGP state path.

- [ ] Step 1: Add failing journal and runtime recovery tests.
  - Tests: test_rejected_zero_fill_entry_is_finalized_without_external_close_diagnosis, test_flat_does_not_release_unresolved_entry_without_terminal_order, test_flat_terminal_zero_fill_releases_entry_lock, and test_partial_or_unknown_entry_blocks_candidate_iteration.
  - Assert a pending/unknown order remains locked and blocked; only terminal zero-fill plus a fresh FLAT read clears it.

- [ ] Step 2: Run the focused recovery tests to verify RED.
  - Run: pytest -q test_recovery_lifecycle.py -k 'entry or pending'
  - Expected: FAIL on the missing pending-entry finalization/reconciliation behavior.

- [ ] Step 3: Add an explicit journal finalizer for zero-exposure entry failures.
  - Implement TradeJournal.finalize_entry_no_exposure(trade_id, reason).
  - It must atomically mark the entry attempt terminal/finalized without recording a fabricated trade close or PnL, and emit a dedicated ENTRY_REJECTED_NO_EXPOSURE event.

- [ ] Step 4: Implement GGP entry outcome adoption and protection.
  - In open_position, treat FILLED and terminal/position-verified PARTIAL_FILL as real exposure and pass their actual filled quantity through the existing native STOP_MARKET placement/read-back.
  - Catch stop placement exceptions after a known fill; preserve the actual order/position facts, set RECOVERY_HOLD, and never release _ACTIVE_TRADE.
  - For EXECUTION_UNCERTAIN, unresolved partials, and ENTRY_PRICE_UNKNOWN, persist attempt identity/order fields, set execution_unresolved, recovery_hold, and RECOVERY_BLOCK_ENTRIES, and retain the lock.
  - Emit actionable stage-specific logs when the lock is already held, balance/ticker is unavailable, order is rejected, or stop verification fails.

- [ ] Step 5: Reconcile pending entries before clearing FLAT recovery.
  - Add a helper that reads the pending client/exchange order ID, requires a terminal zero-fill rejected/canceled/expired order, then performs a fresh exchange position read and requires FLAT.
  - Only after those checks, finalize the journal entry, clear pending-entry flags, and release _ACTIVE_TRADE under _TRADE_LOCK.
  - If no order can be read, status is nonterminal, a fill exists, or position state is UNKNOWN/OPEN, preserve the hold and lock.
  - Apply the same pending-entry distinction during startup so an unresolved ENTRY_PENDING row is not mislabeled as an external close.

- [ ] Step 6: Stop candidate iteration after unresolved execution.
  - Guard the beginning of smart_opportunity_selection against execution_unresolved and recovery_hold.
  - After a failed attempt, break rather than test another symbol whenever a position, recovery hold, unresolved execution, or global recovery block is present.

- [ ] Step 7: Run focused recovery tests and full suite.
  - Run: pytest -q test_recovery_lifecycle.py test_bingx_execution.py
  - Then run: pytest -q
  - Expected: all targeted tests and the full project suite pass with no new warnings/errors.

- [ ] Step 8: Commit Task 2.
  - Commit message: fix: reconcile GGP entry lock and recovery state
