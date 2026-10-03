"""Peak-matching strategy (snapshot-public information only).

Ideas:
1. Cache the per-night window rows the platform publishes (weekly/night_start
   blocks only appear in some snapshots; cache them in memory when present).
2. Every tile must be observed exactly once (practice contract) and its banked
   score scales with atmospheric quality, which peaks at culmination
   (airmass minimum, published per window as best_airmass / best_time_utc).
   So spend each slot on the candidate CLOSEST to its own peak
   (regret = best_airmass / current_airmass), tie-broken by value — the peak
   passes for everyone eventually, and slots outnumber needs.
3. Terminal-pressure rescues first: REQUIRED (and below-quota FLEXIBLE) tiles
   down to their last published window are observed immediately; request
   candidates are taken when live.
4. Hard-weather dodge: skip candidates whose exposure would overlap a
   predicted rain/rocket interval (with the forecast's own uncertainty),
   while an alternative exists.

Modes via .env SAC_STRATEGY_MODE:
  abs       absolute-gain ranking (control ≈ user's v2)
  peak      regret/peak-matching ranking (rescues on)
  peak_dodge  peak + weather dodge
"""
import os
from datetime import datetime, timedelta

MODE = os.environ.get("SAC_STRATEGY_MODE", "peak_dodge")
LAMBDA = float(os.environ.get("SAC_PEAK_LAMBDA", "0.0"))
MISSING_REGRET = float(os.environ.get("SAC_MISSING_REGRET", "0.0"))
LAST_CHANCES = 2
HARD_CONDITIONS = {"rainy", "rocket_launch"}


def _utc(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _update_cache(memory, snapshot, now):
    cache = memory.setdefault("windows", {})
    for block in ("weekly", "night_start"):
        for row in (snapshot.get(block) or {}).get("tile_windows") or []:
            wid = str(row.get("window_id") or "")
            end = _utc(row.get("window_end_utc"))
            if wid and end is not None and end > now:
                cache[wid] = {
                    "tile_id": str(row.get("tile_id")),
                    "start": _utc(row.get("window_start_utc")),
                    "end": end,
                    "best_time": _utc(row.get("best_time_utc")),
                    "best_airmass": float(row.get("best_airmass") or 0.0),
                    "region_id": str(row.get("region_id")),
                    "scheduling_class": str(row.get("scheduling_class")),
                }
    for wid in [k for k, v in cache.items() if v["end"] <= now]:
        cache.pop(wid, None)
    return cache


def _events(memory, snapshot, now):
    cache = memory.setdefault("events", {})
    for f in (snapshot.get("weekly") or {}).get("weather_forecast") or []:
        fid = str(f.get("forecast_id") or "")
        start, end = _utc(f.get("predicted_start_utc")), _utc(f.get("predicted_end_utc"))
        if fid and start is not None and end is not None:
            unc = max(float(f.get("start_uncertainty_seconds") or 0.0),
                      float(f.get("end_uncertainty_seconds") or 0.0))
            cache[fid] = (start - timedelta(seconds=unc), end + timedelta(seconds=unc),
                          str(f.get("condition")), float(f.get("probability") or 0.0))
    for fid in [k for k, v in cache.items() if v[1] <= now]:
        cache.pop(fid, None)
    return list(cache.values())


def _overlaps(candidate, now, events):
    exp = int(candidate.get("nominal_exptime_seconds") or 0)
    span_end = now + timedelta(seconds=exp)
    for start, end, cond, prob in events:
        if cond in HARD_CONDITIONS and prob >= 0.5 and start < span_end and end > now:
            return True
    return False


def choose_action(candidates, snapshot, memory):
    if not candidates:
        return None
    now = _utc((snapshot.get("cursor") or {}).get("timestamp_utc"))
    if now is None:
        return candidates[0]
    cache = _update_cache(memory, snapshot, now)

    chances = {}
    for w in cache.values():
        tid = w["tile_id"]
        chances[tid] = chances.get(tid, 0) + 1

    # terminal-pressure rescues: last published window(s) and still needed
    flexible_done = (snapshot.get("progress") or {}).get("flexible_completed_by_region") or {}
    quota = 4
    try:
        quota = int((snapshot.get("score_config") or {}).get("flexible_quota_per_region", 4))
    except (TypeError, ValueError):
        pass
    rescued = []
    for c in candidates:
        tid = str(c.get("tile_id"))
        n = chances.get(tid, 0)
        cls = (c.get("scheduling_class") or "").upper()
        if cls == "REQUIRED" and 0 < n <= LAST_CHANCES:
            rescued.append((n, 0, c))
        elif (cls == "FLEXIBLE" and 0 < n <= LAST_CHANCES
              and flexible_done.get(str(c.get("region_id")), 0) < quota):
            rescued.append((n, 1, c))
    if rescued:
        rescued.sort(key=lambda item: (item[0], item[1]))
        return rescued[0][2]

    pool = list(candidates)
    if "dodge" in MODE:
        events = _events(memory, snapshot, now)
        survivors = [c for c in pool if not _overlaps(c, now, events)]
        if survivors:
            pool = survivors

    # live requests are taken when ranked on top (deadline pressure via value)
    if "abs" in MODE:
        pool.sort(key=lambda c: -float(c.get("estimated_total_gain") or 0.0))
        return pool[0]
    if "peak" in MODE:
        def regret(c):
            tid = str(c.get("tile_id"))
            cur_am = 0.0
            for cand in snapshot.get("candidate_tiles") or []:
                if str(cand.get("tile_id")) == tid:
                    cur_am = float((cand.get("geometry") or {}).get("airmass") or 0.0)
                    break
            best_am = 0.0
            best_row = None
            for w in cache.values():
                if w["tile_id"] == tid and w["start"] and w["start"] <= now < w["end"]:
                    best_row = w
                    break
            if best_row is not None:
                best_am = best_row["best_airmass"]
            g = float(c.get("estimated_total_gain") or 0.0)
            if best_am <= 0 or cur_am <= 0:
                return (MISSING_REGRET, g)
            r = best_am / cur_am
            if LAMBDA > 0 and g > 0:
                return (r + LAMBDA * min(g, 1500.0) / 1500.0, g)
            return (r, g)
        pool.sort(key=regret, reverse=True)
        return pool[0]
    return pool[0]
