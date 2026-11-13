# Market Intelligence & Trade Lifecycle

## Purpose
This layer classifies a setup's trading mode (SCALP/TREND/SNIPER), primary entry behavior (PULLBACK/MICRO_PULLBACK/RETEST/WAIT_FOR_STRUCTURE), and closed-candle interaction with a reference zone.

## Safety boundaries
- The classifier is pure and execution-free.
- It does not submit, modify, cancel, or replace orders.
- Existing RORO authority remains the entry authorization gate.
- Existing TradeManagementBoard remains the sole routine trade-management decision authority.
- Existing execution service remains the only order path.
- Unknown, stale, malformed, or insufficient inputs fail closed.

## Zone event semantics
- WICK_SWEEP: price crosses the level intrabar but the candle does not close beyond it.
- BREAKOUT_PENDING: one closed candle closes beyond the boundary; acceptance is not yet confirmed.
- BREAKOUT_CONFIRMED: the configured consecutive completed closes are beyond the boundary.
- BREAKOUT_RETEST: accepted breakout followed by a retest touch while price remains accepted.
- FAILED_BREAKOUT: a previous completed candle closed outside, then a later completed candle closes back inside the reference zone.

A wick alone is not a confirmed breakout. These event labels are evidence for the management board and do not force an exit.

## Integration status
The current incremental branch adds an analytical module and tests and passes classification metadata through the institutional bridge and live manager context. It is not a LIVE release. Full pytest/CI, end-to-end paper lifecycle validation, and review of how the live manager consumes zone-event evidence are required before enabling any behavior that changes trade actions.
