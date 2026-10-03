#!/usr/bin/env python3
"""Baseline agent for the v4 survey (participant-agent-protocol-v4). Standard library only.

Reads one JSON object per line on stdin, writes one JSON object per line on stdout, logs to stderr.

  initialize        -> build the planner (catalogue, sky index, night windows)
  decision_request  -> answer with observe / wait / report / finish
  finish            -> print a one-line summary to stderr and exit

Strategy in one paragraph: sleep through the day with one `wait` + `until_utc`; at night point at the
most urgent visible target, fill the other fibres with the most valuable neighbours, and expose just
long enough (planner.py). Close the shutter (wait one slot) while a bulletin says rain or storm over the
whole sky. Report an instrument problem only after a large, lasting drop in quality that no bulletin
explains, at most twice per run.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import timedelta

from llm_hook import LLMAdvisor
from planner import Planner
from skymath import format_utc, parse_utc

PROTOCOL = "participant-agent-protocol-v4"
REPORT_DROP = 0.62          # report when recent clean-sky quality falls below 62% of the earlier level
REPORT_CONFIRMATIONS = 3    # ... on this many checks in a row, on different nights (the drop must persist)
REPORT_SPACING_HOURS = 6.0
MAX_REPORTS = 2


def log(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


class BaselineAgent:
    def __init__(self, init: dict):
        started = time.monotonic()
        self.planner = Planner(init, log=log)
        self.advisor = LLMAdvisor(log=log)
        self.start = parse_utc(init["survey"]["start_utc"])
        # calendar date of each night exactly as the card names it (forecast notices use it)
        self.night_dates = [str(n.get("night_id", ""))[1:] for n in init["survey"]["nights"]]
        self.forecast_notices: list = []
        self.night_seen = None
        self.reports = 0
        self.last_report_hours = -1e9
        self.suspicion = []
        self.observes = 0
        log(f"baseline: {len(self.planner.ids)} targets, {sum(self.planner.required)} required, "
            f"{len(self.planner.nights)} nights; init {time.monotonic() - started:.2f}s; llm={self.advisor.status}")

    def respond(self, payload: dict) -> dict:
        now = parse_utc(payload["now_utc"])
        hours = (now - self.start).total_seconds() / 3600.0
        planner = self.planner
        for message in payload.get("new_messages", []):
            if message.get("record_type") == "forecast":
                self.forecast_notices = message.get("notices", [])
        planner.on_messages(payload.get("new_messages", []), payload.get("latest_bulletin"))
        planner.on_requests(payload.get("active_requests", []), now)
        planner.on_result(payload.get("last_result"), now, hours)
        self._pace(payload, now)

        night = planner.current_night(now)
        if night is None:
            nxt = planner.next_night_start(now)
            if nxt is None:
                return {"action": "finish", "reason": "no observing night left"}
            return {"action": "wait", "until_utc": format_utc(nxt), "reason": "daytime: sleep until the next night"}
        night_index, night_start, night_end = night
        if self.night_seen != night_index:
            self.night_seen = night_index
            self._night_advice(night_start, payload)
        if (night_end - now).total_seconds() < planner.min_exposure:
            nxt = planner.next_night_start(now)
            if nxt is None:
                return {"action": "finish", "reason": "survey over"}
            return {"action": "wait", "until_utc": format_utc(nxt), "reason": "night ending"}
        if planner.site_closed():
            return {"action": "wait", "duration_seconds": self._to_next_slot(now, night_start), "reason": "bulletin: rain/storm over the whole sky"}
        report = self._maybe_report(hours, payload)
        if report is not None:
            return report
        action = planner.plan(now, night_end, night_index, hours)
        if action is None:
            return {"action": "wait", "duration_seconds": self._to_next_slot(now, night_start), "reason": "nothing useful is up"}
        self.observes += 1
        action["reason"] = f"{len(action['assignments'])} fibres, program {action['program']}"
        return action

    def _to_next_slot(self, now, night_start) -> int:
        slot = self.planner.slot_seconds
        into = (now - night_start).total_seconds() % slot
        return int(max(60, min(3600, slot - into)))

    def _night_date(self, night_start) -> str:
        """Calendar date the card itself uses for this night (from night_id)."""
        index = next((k for k, (start, _) in enumerate(self.planner.nights) if start == night_start), None)
        if index is not None and index < len(self.night_dates):
            return self.night_dates[index]
        return night_start.date().isoformat()

    def _pace(self, payload: dict, now) -> None:
        """Do less work per decision when the wall clock is short for the nights still to come."""
        wall = payload.get("wallclock") or {}
        remaining_wall = float(wall.get("remaining_seconds", 1e9))
        night_seconds = sum(max(0.0, (end - max(start, now)).total_seconds()) for start, end in self.planner.nights if end > now)
        decisions_left = max(1.0, night_seconds / max(1, self.planner.slot_seconds))
        per_decision = remaining_wall / decisions_left
        level = 0 if per_decision > 0.12 else 1 if per_decision > 0.04 else 2
        if level != self.planner.fast_level:
            log(f"baseline: pace level {level} ({per_decision * 1000:.0f} ms per decision left)")
            self.planner.fast_level = level

    def _night_advice(self, night_start, payload: dict) -> None:
        self.planner.extra_avoid = set()
        self.planner.duration_scale = 1.0
        if not self.advisor.enabled:
            # deterministic stand-in for the advisor: tonight's forecast notices name the
            # event kind and the compass sectors it will come from
            night_date = self._night_date(night_start)
            for notice in self.forecast_notices:
                if night_date not in notice.get("nights", []):
                    continue
                kind, direction = notice.get("event_kind", ""), notice.get("direction", "")
                if kind in ("rain", "storm", "rocket_launch", "terrain_obstruction"):
                    if direction != "ALL":
                        self.planner.extra_avoid.add(direction)
                elif kind in ("overcast", "cloudy", "haze", "smoggy") and direction == "ALL":
                    self.planner.duration_scale = 0.85   # dimmer sky tonight: shorter exposures
            return
        night_date = self._night_date(night_start)
        tonight = [n for n in self.forecast_notices if night_date in n.get("nights", [])]
        bulletin = (payload.get("latest_bulletin") or {}).get("notices", [])
        left = float((payload.get("wallclock") or {}).get("remaining_seconds", 0))
        advice = self.advisor.night_plan(night_date, tonight, bulletin, left)
        if advice:
            self.planner.extra_avoid = set(advice["avoid_directions"])
            self.planner.duration_scale = advice["duration_scale"]
            log(f"llm night {night_date}: avoid {advice['avoid_directions']} duration x{advice['duration_scale']:.2f}")

    def _maybe_report(self, hours: float, payload: dict):
        """Report only when clean-sky quality dropped a lot and stayed low on three different nights, and
        saturated hits declared DARK do not show that the sky band dropped too (that would be weather)."""
        planner = self.planner
        planner.force_program = None
        if self.reports >= MAX_REPORTS or hours - self.last_report_hours < 24.0:
            return None
        evidence = planner.fault_evidence(hours)
        threshold = REPORT_DROP if self.reports == 0 else REPORT_DROP - 0.07
        if evidence is None or evidence["drop"] >= threshold:
            self.suspicion = []
            return None
        if evidence["dark_checks"] < 6:
            planner.force_program = "DARK"   # diagnostic: ask the sky which band it is in
        elif evidence["dark_matched"] < 0.5 * evidence["dark_checks"]:
            self.suspicion = []              # the sky band dropped too: weather, not the instrument
            return None
        if self.suspicion and hours - self.suspicion[-1] < REPORT_SPACING_HOURS:
            return None
        self.suspicion.append(hours)
        if len(self.suspicion) < REPORT_CONFIRMATIONS:
            return None
        self.suspicion = []
        verdict = self.advisor.confirm_report(evidence, float((payload.get("wallclock") or {}).get("remaining_seconds", 0)))
        if verdict is False:
            log(f"baseline: report vetoed by the model at {payload['now_utc']} ({evidence})")
            self.last_report_hours = hours
            return None
        self.reports += 1
        self.last_report_hours = hours
        planner.forget_quality_history()
        log(f"baseline: report instrument fault at {payload['now_utc']} evidence={evidence}")
        return {"action": "report", "reason": f"quality dropped to {evidence['drop']:.0%} of the earlier level",
                "decision_source": "llm-confirmed" if verdict else "rule"}


class FallbackAgent:
    """Degraded mode when the catalogue cannot be parsed: keep the protocol alive."""

    def __init__(self, init: dict, log=lambda text: None):
        self.planner = None
        self.advisor = type("A", (), {"calls": 0, "enabled": False})()
        self.start = parse_utc(init["survey"]["start_utc"])
        self.nights = [(parse_utc(n["observing_start_utc"]), parse_utc(n["observing_end_utc"]))
                       for n in init["survey"].get("nights", [])]
        self.slot_seconds = int(init["survey"].get("slot_seconds", 900))
        self.observes = 0
        self.reports = 0
        log(f"fallback: survey continues with no science output ({len(self.nights)} nights)")

    def respond(self, payload: dict) -> dict:
        now = parse_utc(payload["now_utc"])
        for start, end in self.nights:
            if start <= now < end:
                into = (now - start).total_seconds() % self.slot_seconds
                return {"action": "wait", "duration_seconds": int(max(60, self.slot_seconds - into)),
                        "reason": "degraded mode"}
        nxt = next((start for start, _ in self.nights if start > now), None)
        if nxt is None:
            return {"action": "finish", "reason": "degraded mode: survey over"}
        return {"action": "wait", "until_utc": format_utc(nxt), "reason": "degraded mode"}


def main() -> int:
    agent = None
    for line in sys.stdin:
        if not line.strip():
            continue
        message = json.loads(line)
        kind = message.get("message_type")
        if message.get("protocol_version") != PROTOCOL:
            log(f"baseline: unexpected protocol {message.get('protocol_version')!r}")
        if kind == "initialize":
            try:
                agent = BaselineAgent(message["payload"])
            except Exception as exc:  # noqa: BLE001 - a broken catalogue must not kill the run
                log(f"baseline: initialize failed ({type(exc).__name__}: {exc}); degraded mode")
                agent = FallbackAgent(message["payload"], log=log)
        elif kind == "decision_request":
            try:
                action = agent.respond(message["payload"])
            except Exception as exc:  # noqa: BLE001 - never crash the run: wait one slot instead
                log(f"baseline: error {type(exc).__name__}: {exc}; waiting one slot")
                action = {"action": "wait", "duration_seconds": 900, "reason": "internal error"}
            advisor_calls = getattr(getattr(agent, "advisor", None), "calls", 0)
            action.setdefault("decision_source", "llm-advised" if advisor_calls else "deterministic")
            print(json.dumps({"protocol_version": PROTOCOL, "message_type": "decision_response",
                              "decision_sequence": message["decision_sequence"], **action}, separators=(",", ":")), flush=True)
        elif kind == "finish":
            payload = message.get("payload", {})
            observes = agent.observes if agent else 0
            reports = agent.reports if agent else 0
            log(f"baseline finished: termination_reason={payload.get('termination_reason')} observes={observes} reports={reports}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
