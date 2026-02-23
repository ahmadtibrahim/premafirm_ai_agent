# IMPLEMENTATION AUDIT – Remediation Plan (Fix Design Only)

This document provides **minimal, safe patch design** for each FAIL item, without introducing new architecture.

## SECTION 1 — PATCH PLAN (P0)

### P0.1 `weather` undefined crash in `_compute_schedule`
- **File + function:** `premafirm_ai_engine/models/crm_lead_extension.py` → `CrmLead._compute_schedule`.
- **Exact call site:** `weather.get_weather_factor(...)` inside the per-stop loop.
- **Root cause:** `weather` is never defined/imported in this scope, so route scheduling can raise `NameError` at runtime before any scheduling writeback.
- **Minimal patch strategy (chosen):** remove runtime weather factor call from scheduling loop. Keep weather persistence handled by existing weather write path in `CRMDispatchService._compute_weather_risk`.
- **Why safe:**
  - Meets constraint: weather must not affect scheduling logic yet.
  - Removes external/weather dependency from stop loop.
  - Does not touch weather storage fields/writes.

**Before snippet (current):**
```python
for stop in ordered:
    travel = mapbox.get_travel_time(prev_loc, stop.address)
    fallback_minutes = float(stop.drive_minutes or stop.drive_hours * 60.0 or 0.0)
    drive_minutes = float(travel.get("drive_minutes") or fallback_minutes)
    distance_km = float(travel.get("distance_km") or stop.distance_km or 0.0)
    weather_info = weather.get_weather_factor(stop.latitude, stop.longitude, when_dt=fields.Datetime.now(), alert_level=lead.weather_alert_level or "none")
    adjusted_minutes = drive_minutes
```

**After snippet (minimal):**
```python
for stop in ordered:
    travel = mapbox.get_travel_time(prev_loc, stop.address)
    fallback_minutes = float(stop.drive_minutes or stop.drive_hours * 60.0 or 0.0)
    drive_minutes = float(travel.get("drive_minutes") or fallback_minutes)
    distance_km = float(travel.get("distance_km") or stop.distance_km or 0.0)
    adjusted_minutes = drive_minutes
```

- **No-regression confirmation:** route times already use `mapbox.get_travel_time`; removing unused weather lookup preserves current ETA math behavior and removes crash path.
- **Tests to add:** yes (unit/integration) to confirm `_compute_schedule` does not call weather service and still schedules stops.

---

### P0.2 Strict window must raise `UserError`
- **File + function:** `premafirm_ai_engine/models/crm_lead_extension.py` → `CrmLead._compute_schedule`.
- **Current behavior:** impossible windows set `conflict = True` silently.
- **Root cause:** strict windows are treated as soft conflicts, so impossible constraints can flow through and write a conflicting schedule.
- **Minimal patch strategy:** change only strict-window violation branch from `conflict = True` to `raise UserError(...)`; keep soft window conflicts as flag-only.

**Exact conditional to update (pattern):**
```python
if end and eta > end:
    conflict = True
```

**Minimal replacement:**
```python
if end and eta > end:
    is_strict = (
        (seg["stop"].stop_type == "pickup" and (lead.strict_pickup_start or lead.strict_pickup_end))
        or (seg["stop"].stop_type == "delivery" and (lead.strict_delivery_start or lead.strict_delivery_end))
    )
    if is_strict:
        raise UserError("Pickup/Delivery window impossible within vehicle constraints.")
    conflict = True
```

- **No-regression confirmation:**
  - Unrelated flows keep existing behavior (`conflict=True`) for non-strict windows.
  - Strict constraints now fail fast as required.
  - Exception is not swallowed and propagates through normal Odoo transaction handling.
- **Test scenario to validate:**
  - Setup lead with strict pickup end earlier than reachable ETA.
  - Run `_compute_schedule()`.
  - Assert `UserError` with required message.
  - Repeat with non-strict window and assert no exception, `schedule_conflict=True`.
- **Tests to add:** yes.

---

### P0.3 Duplicate Mapbox cache methods + schema mismatch
- **File + function:** `premafirm_ai_engine/services/mapbox_service.py`.
- **Root cause:** class contains duplicated definitions of `_get_cache_model`, `_cache_lookup`, `_cache_store`; second set shadows first set. First set uses `waypoints_hash + departure_date`; second set uses `waypoint_hash + departure_hour`.
- **Model schema check:** `premafirm.mapbox.cache` model defines `waypoint_hash` + `departure_hour` (singular + hour integer), so first implementation is mismatched and stale.
- **Minimal patch strategy:**
  - Retain second implementation (`waypoint_hash` + `departure_hour`).
  - Remove shadowed stale implementation (`waypoints_hash` + `departure_date`).
  - Keep call signatures aligned with existing callers (`get_travel_time`, `calculate_trip_segments`).
- **Method removed:** first `_cache_lookup/_cache_store/_get_cache_model` block (stale schema).
- **Method retained:** second `_cache_lookup/_cache_store/_get_cache_model` block (model-consistent schema).
- **No-regression confirmation:** runtime currently already uses shadowed (second) methods; deleting dead duplicate is behavior-preserving while removing confusion.
- **Tests to add:** yes (cache hit/miss and persistence key assertions).

## SECTION 2 — PATCH PLAN (P1)

### P1.4 Move capacity validation before scheduling
- **File + function:** `premafirm_ai_engine/models/crm_lead_extension.py` → `CrmLead._compute_schedule`.
- **Root cause:** capacity check runs after route segments and ETA updates are computed.
- **Minimal patch strategy:** move weight/pallet validation block to immediately after `ordered`/`vehicle` are resolved and before first `mapbox.get_travel_time` call.
- **New validation location:** top of per-lead loop, before segment loop.
- **No scheduling before validation confirmation:** with reordered block, any over-capacity lead raises `UserError` before segment computation/writes.
- **Tests to add:** yes.

### P1.5 Remove per-stop weather calls
- **File + function:** `premafirm_ai_engine/models/crm_lead_extension.py` → `CrmLead._compute_schedule`.
- **Calls identified:** `weather.get_weather_factor(...)` in stop loop.
- **Minimal patch strategy:** remove call from runtime scheduling path (same change as P0.1).
- **Forecast storage preserved:** keep `CRMDispatchService._compute_weather_risk` write path for `weather_*` fields unchanged.
- **Tests to add:** optional if covered by P0.1 schedule test + weather-risk storage test.

### P1.6 Enforce `return_home=True` consistently
- **File + function:** `premafirm_ai_engine/services/run_planner_service.py` → `RunPlannerService.simulate_run`.
- **Current path:** `calculate_trip_segments(..., return_home=False)`.
- **Minimal patch strategy:** flip to `return_home=True` in simulation primary call.

**Diff snippet (minimal):**
```diff
- segments = self.map_service.calculate_trip_segments(home, ordered, return_home=False)
+ segments = self.map_service.calculate_trip_segments(home, ordered, return_home=True)
```

- **ETA chaining regression check:** ETA loop still indexes per stop (`segments[idx]`), so extra terminal home segment is ignored for stop ETA and only affects run-distance realism. No stop ETA chaining regression expected.
- **Tests to add:** yes (home-return segment included in aggregate distance).

## SECTION 3 — PATCH PLAN (P2)

### P2.7 Ensure no `None` `scheduled_datetime`
- **File + function:** `premafirm_ai_engine/models/crm_lead_extension.py` → `CrmLead._compute_schedule`.
- **Audit result:** current branches set `scheduled_datetime=eta`, but defensive fallback is still useful for future branch edits/manual paths.
- **Minimal patch strategy:** enforce before writing stop vals:
```python
if not eta:
    eta = vehicle_start
```
or post-compute fallback:
```python
scheduled_datetime = eta or vehicle_start
```
- **No-regression confirmation:** only activates when ETA unexpectedly missing; default behavior unchanged.
- **Tests to add:** yes (force missing ETA path via crafted fixture/mocking).

### P2.8 Cron-safe scheduling
- **Candidate location:** `CrmLead._compute_schedule` loop (already iterates recordset and acts as batch entry point from stop create/write/unlink).
- **Minimal patch strategy:** wrap each lead iteration in `try/except Exception` and log; continue next lead.
- **No swallowing for strict errors requirement:** re-raise `UserError` (business validation), only continue on unexpected exceptions.
- **Behavior:** batch continues for other records; individual lead failures remain visible in logs.
- **Tests to add:** yes (one lead forced to fail; next lead still scheduled).

### P2.9 `tzdata` documentation
- **File:** `README.md`.
- **Minimal patch strategy:** add dependency note under installation/dependencies:
  - `pip install tzdata` for environments lacking IANA timezone data.
- **Fallback logic check:** present via company timezone default and explicit fallback (`America/Toronto`) in `_vehicle_start_datetime`.
- **Tests to add:** optional documentation-only; timezone functional tests still recommended.

## SECTION 4 — DUPLICATION DECISION

- **Two schedulers:**
  1. `crm.lead._compute_schedule` (`crm_lead_extension.py`)
  2. `CRMDispatchService._compute_stop_schedule` (`crm_dispatch_service.py`)

### Recommendation (minimal-risk)
- **Canonical:** `crm.lead._compute_schedule`.
  - It is already the method invoked by stop model create/write/unlink hooks and is integrated with strict windows, conflict flagging, lead-level leave-yard fields.
- **Legacy wrapper:** mark `CRMDispatchService._compute_stop_schedule` as legacy helper in docstring and avoid introducing new callers.
- **Prevent double logic:** keep `_apply_routes()` using only `lead._compute_schedule()` (already does), and do not call `_compute_stop_schedule` from main flow.

## SECTION 5 — TEST PLAN

> Design-only plan (no full test implementations yet).

1. **Strict pickup window**
   - **Function name:** `test_compute_schedule_raises_usererror_for_impossible_strict_pickup_window`
   - **Setup:** lead with strict pickup end before reachable ETA.
   - **Assert:** `_compute_schedule` raises `UserError` with required text.

2. **Strict delivery window**
   - **Function name:** `test_compute_schedule_raises_usererror_for_impossible_strict_delivery_window`
   - **Setup:** feasible pickup, impossible strict delivery end.
   - **Assert:** `UserError` raised.

3. **After 13:00 rule**
   - **Function name:** `test_vehicle_start_datetime_rolls_to_next_day_after_1300`
   - **Setup:** freeze now at 13:05 local timezone.
   - **Assert:** `_vehicle_start_datetime()` date is next day at work-start hour.

4. **Capacity exceed**
   - **Function name:** `test_compute_schedule_fails_fast_when_vehicle_capacity_exceeded`
   - **Setup:** sum pallets/weight above vehicle limits.
   - **Assert:** `UserError` before mapbox call (spy/mock ensures no routing calls).

5. **Cache hit vs miss**
   - **Function name:** `test_mapbox_cache_lookup_hit_and_miss_use_waypoint_hash_departure_hour`
   - **Setup:** seed `premafirm.mapbox.cache`; call `_cache_lookup` with matching/nonmatching keys.
   - **Assert:** hit returns metrics; miss returns `None`; schema keys match model fields.

6. **Home location enforced**
   - **Function name:** `test_run_planner_simulation_includes_return_home_segment`
   - **Setup:** run with N stops and known stub travel segments.
   - **Assert:** simulation route includes terminal home leg (distance increases accordingly) while stop ETAs stay chained.

7. **Timezone safety**
   - **Function name:** `test_vehicle_start_datetime_uses_company_timezone_fallback`
   - **Setup:** unset/invalid partner tz fallback path.
   - **Assert:** schedule start resolves using fallback timezone without crash.

## SECTION 6 — RISK ANALYSIS AFTER FIXES

- **Low risk:** removing undefined weather call and duplicate dead cache methods (stability/clarity fixes).
- **Medium risk:** strict-window `UserError` behavior change could surface previously silent impossible loads; mitigated by strict-only guard.
- **Medium risk:** moving capacity check earlier changes failure timing (desired fail-fast). Ensure UI message clarity.
- **Low-medium risk:** `return_home=True` affects run-level totals; ETA sequencing remains stable due to index logic.
- **Operational risk mitigation:** add targeted tests above and run existing dispatch/scheduler suites to confirm no regression.
