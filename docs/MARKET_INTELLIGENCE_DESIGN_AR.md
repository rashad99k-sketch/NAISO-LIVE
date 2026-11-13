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

## Runtime wiring
- The institutional bridge computes Wilder ADX from the validated, completed-candle frame; missing/insufficient ADX remains unknown.
- A qualified causal Order Block is the primary reference zone. A directionally valid, unfilled FVG is the fallback. Invalid or missing bounds are not fabricated.
- Mode, entry behavior, confidence, reasons, and reference bounds are copied into the entry context for both the unified institutional path and SNIPER V2 path, then persisted in the active trade state.
- The live manager uses the canonical `last_closed_ts` and `_closed_candle_frame`; the observer only evaluates completed candles and deduplicates by candle timestamp.
- A failed breakout is latched only when a close re-enters the zone after an outside close, then confirmed when the next adjacent completed candle remains inside. A duplicate, unrelated event, or time gap resets the streak.
- The management board may consume this evidence once per trade, only when it is fresh, same-side, confirmed, confidence >= 0.75, and streak >= 2. It is supporting thesis evidence, not a direct close command.

## Integration status
The mode label and entry behavior are analytical metadata; they do not replace or bypass the existing RORO entry authority. TradeManagementBoard remains the single routine management decision authority and the existing execution/verification pipeline remains the only order path. This branch is still a review candidate, not a LIVE release. Passing CI does not replace end-to-end PAPER validation of a full entry-to-close lifecycle.


## Closed-candle chart pattern observer
- BULL_FLAG / BEAR_FLAG require a measured impulse, a bounded flag retracement, and closed-candle breakout confirmation for the confirmed stage.
- FLAT_TOP / FLAT_BOTTOM require repeated boundary tests and a directionally converging base (higher lows under a flat top; lower highs above a flat bottom).
- Patterns are emitted as observational context with stage, direction, confidence, reference level, and reason codes.
- These pattern labels do not independently authorize entries, change sizing, modify protection, or issue exits. The existing institutional authority and management board retain those responsibilities.

## Mode semantics and operator visibility
- trading_mode is the entry/setup identity: SNIPER requires a side-aligned liquidity sweep, a same-direction CHoCH/MSS, and a valid causal OB/FVG zone; TREND requires directional regime, ADX and continuation confirmation; SCALP requires a confirmed range/weak regime plus adequate liquidity. Otherwise the entry mode stays UNCLASSIFIED.
- profit_mode is the separate, live management policy: RANGE_SCALP, TREND_RIDE, or the conservative TWO_STAGE_50_50 fallback. It is re-evaluated on completed candles and stabilized before switching.
- Management events and logs expose both the immutable entry mode and the current management mode. This is observability only; it does not alter order sizing, leverage, TP/SL, or execution.
- A BOS continuation is not a reversal MSS. Opposite-side sweeps, opposite-direction CHoCH, and opposite-direction FVGs cannot qualify a SNIPER setup.
