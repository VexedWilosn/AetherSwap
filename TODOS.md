# TODOs

## SteamDT OpenAPI Follow-Ups

### P1 - Add operator-visible manual sync and health reporting
- What: Add a manual trigger and health summary for SteamDT OpenAPI base mapping sync.
- Why: When a mapping drift incident appears, operators need a first-class way to confirm whether the scheduled sync is healthy before touching code.
- Context: The current phase intentionally keeps sync in backend maintenance paths only. Once the mapping-first rollout lands, the next operational gap will be observability and manual recovery.
- Depends on / blocked by: Phase 1 SteamDT OpenAPI client and daily sync implementation.

### P1 - Design quote-class separation before persisting supplemental quotes
- What: Define how execution-grade JIT quotes and supplemental SteamDT quotes can coexist without overwriting each other.
- Why: `market_price` is unique on `(item_id, platform_name)`, so blindly persisting SteamDT OpenAPI quotes would risk silent data contamination.
- Context: The reviewed plan explicitly avoids writing OpenAPI supplemental quote data into `market_price`. Any future attempt to use SteamDT price endpoints beyond runtime-only diagnostics must solve this first.
- Depends on / blocked by: none, but should be decided before any persistence work on `price_single`, `price_batch`, or `price_avg`.

### P2 - Evaluate OpenAPI market signals for priority scheduling
- What: Benchmark `price_avg`, `item_kline`, and `broad_index` as optional inputs to `priority_scheduler`.
- Why: These endpoints may improve crawl prioritization or market-regime awareness with lower operational cost than source-by-source probing.
- Context: This is intentionally deferred until mapping sync is stable and we have a clearer view of signal freshness and reliability relative to existing strategy inputs.
- Depends on / blocked by: stable phase 1 mapping sync and signal freshness benchmarking.

