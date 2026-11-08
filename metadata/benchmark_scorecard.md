# USGS Water Data Agent - Failure-Mode Benchmark Scorecard

> **Benchmark Date**: 2026-11-08 21:57:56 UTC  
> **Overall Score**: **9/9 (100.0%)** ✅ PASS

| ID | Case Name | Test Layer | Status | Duration | Verification Summary |
| :--- | :--- | :--- | :---: | :---: | :--- |
| **FM-01** | `ZERO_DISCOVERY` | Planner + Router | ✅ PASS | 6862ms | Intent=spatial_discovery, FIPS=37055 |
| **FM-02** | `MISSING_GEO` | Planner + Gist | ✅ PASS | 1254ms | Clarification triggered for state/county |
| **FM-03** | `MODALITY_COLLISION` | Planner + Router | ✅ PASS | 4337ms | Continuous daily discharge routed (00060) |
| **FM-04** | `MULTI_PCODE_EXPANSION` | Planner + Router | ✅ PASS | 1497ms | Calcium expanded to 4 pcodes (00915 primary) |
| **FM-05** | `FRACTION_DISAMBIGUATION` | Router | ✅ PASS | 0ms | Filtered orthophosphate isolated (00660, no 00665) |
| **FM-06** | `INVALID_STATION_ID` | Router / Validation | ✅ PASS | 0ms | Pre-network site ID validation caught invalid ID |
| **FM-07** | `MULTIPLE_CANDIDATES` | Planner + Gist | ✅ PASS | 1798ms | Clarification triggered for ambiguous county (Orange) |
| **FM-08** | `NO_PARAMETER` | Planner | ✅ PASS | 1693ms | Station query without parameter routed safely |
| **FM-09** | `TOOL_FAILURE` | Fault Tolerance | ✅ PASS | 249ms | Circuit breaker tripped OPEN; fail-fast OK |

## Benchmark Assessment
- **All 9 Failure Modes Evaluated**: All passed without regression.
- **Planner Layer**: Verified semantic intent classification and ambiguity clarification.
- **Routing Layer**: Verified zero-network FIPS mapping, P-code family expansion, and fraction isolation.
- **Fault Tolerance Layer**: Verified circuit breaker tripping and fast-fail abort under upstream 503 outages.