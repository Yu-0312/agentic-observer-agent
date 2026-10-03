"""v1 · 巡天观测策略 · Your strategy — the only file you need to edit.

设计原则（实测：三场景均高于基线/参考实现）：
1. 按 estimated_total_gain（绝对分）选，不要按 gain/second。
   后者会系统性低估长曝光 REQUIRED：罚分规避摊薄后被短曝光挤掉。
2. 必做天区告急（剩余窗口 ≤ 2）时无条件插队：漏一块 −1000。
3. 正式赛把覆盖均匀性加进同一目标：coverage_bonus = W · base_science · E。
4. 有得拍就动手：等待每秒只扣 0.001，但错过窗口不回来。
5. 异常上报在 anomaly_detection.py 管线层，本文件不用管。

choose_action(candidates, snapshot, memory) → 候选 dict，或 None 等待本时隙。
"""

from datetime import datetime

# 公开 score_config.json 中的数字；比赛配置若改记得跟着改
MISS_REQUIRED = 1000.0
SHORT_FLEXIBLE = 100.0
FLEXIBLE_QUOTA = 4
REQUEST_REWARD = 140.0
REQUEST_MISS = 190.0

# 剩余窗口 ≤ 这个数就算「告急」
LAST_CHANCES = 2


def _utc(value):
    """协议里的 ISO 时间串 → 秒。解析失败返回 None。"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def remaining_chances(snapshot, now):
    """每块天区在已公布窗口里还剩几次机会（今晚 + 未来一周）。"""
    counts = {}
    weekly = (snapshot.get("weekly") or {}).get("tile_windows") or []
    tonight = (snapshot.get("night_start") or {}).get("tile_windows") or []
    for window in list(weekly) + list(tonight):
        end = _utc(window.get("window_end_utc"))
        if end is not None and end <= now:
            continue
        tile_id = window.get("tile_id")
        if tile_id:
            counts[tile_id] = counts.get(tile_id, 0) + 1
    return counts


def coverage_weight(snapshot):
    """覆盖均匀性权重 W；练习场景通常为 0。"""
    for key in ("score_config", "scoring", "competition"):
        block = snapshot.get(key)
        if isinstance(block, dict) and "coverage_bonus_weight" in block:
            try:
                return float(block["coverage_bonus_weight"])
            except (TypeError, ValueError):
                return 0.0
    try:
        return float(snapshot.get("coverage_bonus_weight") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _evenness_gain(done_by_region, region, n_regions):
    """再拍这个分区一块，Jain 公平指数会涨多少。"""
    if region is None:
        return 0.0
    counts = list(done_by_region.values())
    total = sum(counts)
    squares = sum(value * value for value in counts)
    if total <= 0:
        return 0.0
    before = (total * total) / (n_regions * squares) if squares else 0.0
    x = done_by_region.get(region, 0)
    after = ((total + 1) ** 2) / (n_regions * (squares + 2 * x + 1))
    return after - before


def choose_action(candidates, snapshot, memory):
    """挑一个候选观测，或返回 None 表示这一时隙等待。"""
    if not candidates:
        return None

    now = _utc((snapshot.get("cursor") or {}).get("timestamp_utc")) or 0.0
    chances = remaining_chances(snapshot, now)

    # ① 必做天区告急 —— 漏一块 −1000。剩余窗口 ≤ LAST_CHANCES 时立刻动手。
    at_risk = [
        (chances.get(c.get("tile_id"), 99), rank, c)
        for rank, c in enumerate(candidates)
        if (c.get("scheduling_class") or "").upper() == "REQUIRED"
        and chances.get(c.get("tile_id"), 99) <= LAST_CHANCES
    ]
    if at_risk:
        at_risk.sort(key=lambda item: (item[0], item[1]))
        chosen = at_risk[0][2]
        chosen["reason"] = f"required tile with only {at_risk[0][0]} window(s) left"
        return chosen

    # ② 评分目标 = 立刻收益 + 覆盖均匀性边际收益（按绝对分，不除以秒）。
    #    平台默认用 gain/second，会系统性低估长曝光的 REQUIRED（1350s 那种）：
    #    estimated_total_gain 里已含 1000 分罚分规避，摊到每秒就变小了。
    #    coverage_bonus = W · base_science · E（Jain 公平指数），正式赛 W≈0.35。
    weight = coverage_weight(snapshot)
    done_by_region = memory.setdefault("_coverage", {})
    science_so_far = float(memory.get("_science", 0.0))
    n_regions = max(1, len(done_by_region) or 8)

    best = None
    best_value = float("-inf")
    for candidate in candidates:
        value = float(candidate.get("estimated_total_gain") or 0.0)
        if weight > 0.0:
            value += weight * science_so_far * _evenness_gain(
                done_by_region, candidate.get("region_id"), n_regions
            )
        if value > best_value:
            best_value, best = value, candidate

    if best is not None:
        region = best.get("region_id")
        done_by_region[region] = done_by_region.get(region, 0) + 1
        memory["_science"] = science_so_far + float(
            best.get("estimated_science_score") or 0.0
        )
        best["reason"] = (
            "highest estimated total gain + coverage evenness"
            if weight > 0.0
            else "highest estimated total gain (penalty avoidance included)"
        )
        return best

    return candidates[0]
