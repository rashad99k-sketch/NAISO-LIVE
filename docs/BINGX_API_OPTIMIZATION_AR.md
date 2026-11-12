# NAISO — BingX API and Market Data Reliability

## Scope and invariants

This release hardens the data and API infrastructure around the existing entry and execution paths. It does not change strategy signals, scanner ranking, order-block logic, structural SL/TP calculations, leverage policy, TP1/TP2, profit management, or exit decisions.

The existing 20x leverage configuration and read-back remain in place. MARKET and protection writes remain single-shot; an ambiguous write is reconciled rather than blindly replayed.

## Market data: WebSocket first, REST fallback

- USDT-M public market stream: \`wss://open-api-swap.bingx.com/swap-market\`.
- `{symbol}@bookTicker` supplies best bid/ask for spread checks.
- `{symbol}@depth{level}@{interval}` supplies limited-depth snapshots for the existing liquidity and order-book imbalance calculations. The feed uses 200ms for BTC/ETH and 500ms for other USDT-M symbols.
- Depth snapshots contain complete bid/ask arrays and are replaced atomically on each message. The client does not merge `incrDepth` deltas.
- The client handles GZIP payloads, Ping/Pong, paced subscriptions, a local 150-topic cap with least-recently-used eviction, reconnect with bounded exponential backoff, and local receive-age validation.
- Only symbols queried by the existing candidate/watchlist/radar code are subscribed; the full scanner universe is not preloaded.
BookTicker snapshots older than `BINGX_BOOK_TICKER_STALE_AFTER_SEC` and depth snapshots older than `BINGX_DEPTH_STALE_AFTER_SEC` by local receive time are rejected. On disconnect/reconnect, local books are invalidated until fresh messages arrive.
- BingX documents the limited-depth `ts` as server message-construction time, not matching-engine/source-book age. The depth snapshot is therefore marked `source_age_verified=false`; local receive-age validation does not prove upstream source age. BookTicker's exchange timestamp is retained separately.
- Scanner depth reads do not call REST by default. Set `BINGX_REST_DEPTH_FALLBACK_ENABLED=true` only if REST depth fallback is deliberately preferred; execution spread preflight retains a rate-limited REST fallback.
- Full `incrDepth` is deliberately not used. Its separate initialization/recovery protocol is unnecessary for the current bounded depth indicators and must not be approximated with an unsynchronized local merge.

## REST traffic control

CCXT's built-in rate limiter remains enabled. A shared application-level controller adds:

- Bounded concurrency for read requests.
- Endpoint pacing at 1.05 seconds for the explicitly documented 1 request/second/IP methods: REST depth, ticker, klines, recent trades and contract catalogue reads. Other methods retain the shared CCXT/application limiter unless their endpoint-specific rate is documented.
- A shared cooldown for HTTP 429, HTTP 418, BingX 100410, 109403, 109429 and 109500 responses.
- One bounded exponential retry with jitter for safe transient read failures; no automatic retry for order writes or unknown methods.
- A 418 cooldown defaults to 15 minutes and respects a longer Retry-After header without shortening the server's requested wait. New entries are explicitly blocked during the API circuit cooldown; close/protection writes are not gated by this entry-only check. Do not treat cooldown expiry as proof that an IP ban has been lifted; verify API recovery before enabling new entries.

The endpoint limiter is process-local. Run one bot process/replica per API identity unless a distributed limiter is introduced.

## Coalescing and cache boundaries

- Passive position snapshots may be shared for up to 10 seconds; UNKNOWN may be shared for up to 5 seconds.
- Execution-critical reads keep \`force=True\` by default. Close submission, close verification, recovery and other order-critical checks bypass the passive cache.
- Position caches are invalidated before non-idempotent close writes and after entry exposure is acknowledged.
- Realized PnL is accounting/dashboard data, not a per-tick execution signal. Successful reads are cached for 30 seconds; failed reads preserve the last good value and use a bounded backoff. Verified closes invalidate the cache.
- Duplicate concurrent REST depth, ticker, OHLCV and total/free balance reads for the same cache key share one fetch.

## Position sizing diagnostics

The normal strategy path defaults to BingX Futures **Free/Available USDT**, after the configured balance safety factor. It can be explicitly switched with `POSITION_SIZING_BALANCE_BASIS=WALLET_TOTAL`; the final exchange free-margin check remains mandatory. Sniper V2 keeps its separate, explicit Wallet Total basis. The default is unchanged.

Before sizing, the structural SL is rounded with BingX market precision and its direction is revalidated. At the final LIVE preflight, stop risk is recalculated from the latest quote, precision-rounded SL, precision-rounded quantity, contract size and execution buffer. A primary entry is blocked if this actual pre-submit risk exceeds its existing risk budget; quantity is never expanded to force the target margin.

The 20% or 30% allocation is a target margin ceiling, not a guaranteed spend. \`MAX_TRADE_RISK_PCT\` limits estimated stop loss as a percentage of that target margin. A wide structural stop therefore reduces the permitted notional and actual margin.

Illustration at 20x:
- Effective sizing balance: 33 USDT.
- TREND target margin: 6.60 USDT.
- Risk cap: 10% of target margin = 0.66 USDT.
- Entry: 100; structural stop: 97; execution buffer: 0.05%.
- Estimated allowed margin is approximately 1.08 USDT, not 6.60 USDT.

If Wallet Total is 33 USDT but Free USDT is 7.50, a normal 20% allocation starts from 7.50 (then applies the safety factor), so its target is approximately 1.50 USDT before any stop-risk reduction. New logs show Wallet Total, Free USDT, safety factor, effective sizing balance, target margin and post-fill stop-risk reconciliation.

The post-fill reconciliation is diagnostic only: it reports whether the actual filled quantity and average price moved estimated stop risk beyond the sizing budget. It does not widen, move or replace the structural stop, close the trade, or alter the trade manager.

## Hosted runtime and validation

- The existing hosted readiness work separates process liveness from trading readiness. A live process may remain healthy while BingX is temporarily unavailable; new entries remain blocked until preflight and recovery complete.
- Use one process/replica and persistent storage for the trade journal.
- Validate in PAPER, then BingX Demo/VST. GitHub Actions mocks do not prove network quality, API permissions, exchange-native SL behavior, or 24/7 provider stability.
- Before LIVE, record REST request counts by method, WS feed age/reconnects, 429/418/109500 counts, entry preflight latency, and Native SL verification latency during a sustained soak test.


## Deployment boundary

The API scheduler, caches, single-flight locks and WebSocket subscriptions are process-local. Run one NAISO worker/replica per BingX egress IP. If the service is horizontally scaled, a shared distributed rate limiter and shared state are required; this PR does not claim cross-process coordination.