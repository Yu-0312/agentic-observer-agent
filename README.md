# Agentic Observer — survey agent (GOSIM 2026 巡天智能体)

Complete-project submission for the GOSIM 2026 *Agentic Observer* hackathon
(https://create.gosim.org/survey26/), built on the official **v4** starter-kit agent
(`participant-agent-protocol-v4`). The platform runs this repository as-is:

```
python3 -u baseline_agent.py
```

with the manifest in [`observer.project.json`](observer.project.json)
(`python:3.12-slim`, `protocol: jsonl-v4`, no build step, no model key required — `USE_LLM=0`).

## What it does

v4 task: run a spectroscopic survey from a virtual Paranal telescope. Every decision picks one
pointing (alt/az), up to 16 fibre→target assignments, an exposure length (60–3600 s) and a program
(DARK / BRIGHT / BACKUP). Only each target's best exposure counts; required targets below a 0.5
completion factor cost 50 points each; uneven per-RA-band coverage is penalised (Jain index).

Beyond the official baseline, this submission adds three data-driven changes to `planner.py`
(tuned against the organizers' card generator on five same-size proxy cards, scored with the
official v4 runner):

1. **Forecast-aware night advice** (deterministic, replacing the dormant LLM advisor): tonight's
   forecast notices steer pointings away from directional rain/storm/rocket sectors and shorten
   exposures under all-sky overcast. (+~300 on proxy cards; the real-card runs lost 87–142
   observations to weather interruptions.)
2. **Request floor**: time-limited observation requests are all-or-nothing; partial completion
   progress now keeps request targets scheduled instead of starving them until the deadline.
3. **Fresh-capacity field navigation**: anchors are weighted by how many never-observed targets
   their field can still fill the 16 fibres with (top teams observe 93–99% of the catalogue;
   breadth, not depth, wins).

The agent (official baseline framework, `planner.py`):

1. sleeps through daytime with one `wait` + `until_utc`;
2. ranks visible targets by remaining gain, urgency (nights left, setting) and request rewards;
3. tries several "anchor" pointings and fills the 16 fibres with the most valuable neighbours;
4. picks the exposure length with the best expected score per second and the program that most
   assigned targets will match;
5. learns the current sky quality from its own results (never reads hidden weather), dodges
   bulletin/forecast hazards (rain, storms, rocket launches, terrain obstruction);
6. reports an instrument fault only after a large, persistent, unexplained quality drop
   (rule-based, optionally LLM-confirmed via `llm_hook.py` — disabled in this submission).

## Results (official v4 local runner)

| Card | Score | Notes |
|---|---|---|
| `cards/demo` (7 nights, 2 400 targets) | **1161–1343** | official baseline reference: 1082.57 |
| alpha-scale generated cards (38 nights, 10 000 targets, 500 required) | **5 477 – 6 570** (mean of 5 seeds ≈ 5 800) | official baseline on the same cards: mean ≈ 5 546; first platform run on card α: 3 616.59 |

The alpha-scale card was generated locally with the organizers' card generator at the real
practice-card α parameters; the real α card's weather stays private, so scores there will differ.

## Repo layout

- `baseline_agent.py`, `planner.py`, `skymath.py`, `llm_hook.py`, `observer.project.json` —
  the v4 agent (from the official starter kit, `agent/` folder flattened to the repo root)
- `legacy_v3/` — our previous challenge-v3 agent (tile-based protocol), including the
  peak-matching strategy that scored **6907.87** on `dev-fortnight` and **13110.83** on
  `dev-reference` on the v3 debug board; kept for reference only

## License

MIT — see [LICENSE](LICENSE). Challenge data, task cards, evaluation code and example
projects are provided by the organizers under CC BY-NC 4.0.
