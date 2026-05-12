# TODOs

## SteamDT OpenAPI Follow-Ups

### P1 - Add UI/API manual force sync for SteamDT OpenAPI base mapping
- What: Add an operator-facing button or API endpoint that calls `DataEngine/steamdt_openapi.py --force` and returns the latest mapping sync stats.
- Why: When a mapping drift incident appears, operators need one-click recovery without opening a shell or waiting for the scheduled sync window.
- Context: Daily base mapping sync, runtime state, and platform connectivity health are now implemented. The remaining gap is a first-class manual force trigger for the base mapping sync itself.
- Depends on / blocked by: none; reuse `sync_steamdt_openapi_base(force=True)` and surface `config/steamdt_openapi_state.json` / `config/platform_runtime_state.json` in the response.

### P2 - Revisit quote-class separation if execution and supplemental feeds need parallel lanes
- What: Decide whether `market_price` needs a separate quote-class key once execution-grade JIT quotes and supplemental SteamDT OpenAPI quotes must coexist at the same time.
- Why: `market_price` is still unique on `(item_id, platform_name)`. The current implementation stores the freshest current quote per platform with `data_source='steamdt_openapi'`, which is sufficient for radar and priority scheduling but not for preserving multiple simultaneous quote lanes.
- Context: SteamDT OpenAPI price persistence now uses `upsert_market_price_if_fresh()`, real `updateTime`, CNY filtering, liquidity fields, and outlier filtering. A separate quote-class table or unique key is only needed if later execution logic must compare source classes side by side.
- Depends on / blocked by: a concrete execution use case that requires simultaneous per-source quote history.

## Completed

### P2 - Evaluate OpenAPI market signals for priority scheduling
- **Completed:** multi-platform automation core (2026-05-10)
- What shipped: `DataEngine/steamdt_openapi_price.py` persists OpenAPI orderbook depth and liquidity signals, and `DataEngine/priority_scheduler.py` uses those signals for P2/P3 prioritization and cashout opportunity tiers.
- Evidence: covered by `tests/test_runtime_hardening.py`, `tests/test_radar_snapshot.py`, and `tests/test_steamdt_openapi_price.py`.

