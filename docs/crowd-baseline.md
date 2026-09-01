<!--
  SPDX-License-Identifier: MPL-2.0
  This Source Code Form is subject to the terms of the Mozilla Public
  License, v. 2.0. If a copy of the MPL was not distributed with this
  file, You can obtain one at https://mozilla.org/MPL/2.0/.
-->
# Crowd online baseline (C3)

Seasonal + rolling EWMA baseline, floored-MAD scale, episode freeze,
and hold-and-backfill for the crowd detector. Frozen numbers live in
ADR-0008 and `docs/crowd.md`; C1 owns the helpers; this module owns
the online state machine. C4's backend calls it each window.

## Module

`blesentry.detection.crowd_baseline.CrowdBaseline`

## Per-window flow

1. Select **tier** (seasonal vs rolling) from wall-clock trust and cold
   start (below).
2. Read **baseline** from the active tier *before* updating.
3. **Residual** = `count_near − baseline`.
4. **Scale** = `floored_mad(residuals)` on the tier's capped residual
   window (`CROWD_RESIDUAL_WINDOW`, same span as EWMA).
5. **z** = `residual / scale`.
6. Unless `in_episode` (CUSUM `S > 0`), update EWMA / rolling **and**
   append the residual to the capped window. During an episode the
   pre-episode residual history alone defines scale; the live residual
   is excluded from `floored_mad`. The active tier is pinned for the
   episode so cold-start completion cannot switch rolling → seasonal
   mid-episode.

Episode freeze and hold-and-backfill match ADR-0008 / DC-4.

**Install age** anchors on the first **trusted** wall-clock sample.
Untrusted timestamps never seed `_install_at`. A backward NTP step
resets the anchor to the corrected time so age cannot go negative. A
forward jump of at least ``CROWD_COLD_START_HOURS`` between consecutive
trusted samples re-anchors install age (prevents NTP/epoch sync from
bypassing cold start).

**Seasonal training during cold start:** while the active read tier is still
rolling, trusted observations continue to train hour-of-week EWMA buckets
and per-bucket residual history (against the bucket EWMA, not the
rolling mean) in the background. Unvisited buckets at
switchover fall back to the rolling mean / rolling residual scale.

## Tiers (DC-4)

| Tier | When | Baseline |
|---|---|---|
| **Seasonal** | Wall clock trusted **and** trusted operating hours ≥ `CROWD_COLD_START_HOURS` (168 h) | Hour-of-week EWMA (`CROWD_HOUR_OF_WEEK_BUCKETS` = 168), α = `ewma_alpha(CROWD_EWMA_SPAN)` |
| **Rolling** | Otherwise | Mean of last `CROWD_ROLLING_WINDOWS` `count_near` samples (~7 d at 15 s) |

**Hold-and-backfill:** while wall clock is untrusted, seasonal buckets are
not updated; `(observed_at, count_near)` pairs queue. When trust flips
true **and** the detector is not in an episode, the queue drains in
chronological order immediately (even during cold start — seasonal
updates only; rolling already ran live). A clock re-anchor discards any
queued samples. Rolling is always updated on the live path.

**Cold start:** seasonal is not selected until **trusted operating hours**
(accumulated wall-clock time between consecutive trusted samples with
gaps ≤ 24 h) reach ``CROWD_COLD_START_HOURS``. Backward steps, forward
jumps above 24 h, or ≥ 168 h between trusted samples reset the
accumulator.

## Residual window cap (DC-2)

`CROWD_RESIDUAL_WINDOW` = `CROWD_EWMA_SPAN` (56). One deque per seasonal
bucket plus one for the rolling tier. Steady-state RSS is O(buckets × cap),
not unbounded history.

## API

```text
hour_of_week(observed_at) -> int   # 0..167, Monday 00:00 UTC

CrowdBaseline.observe(
    count_near,
    observed_at,
    *,
    wall_clock_trusted: bool,
    in_episode: bool,
) -> BaselineStep

CrowdBaseline.begin_window(observed_at, *, wall_clock_trusted, in_episode) -> None
CrowdBaseline.preview(...) -> BaselineStep
CrowdBaseline.commit(..., tier) -> None            # EWMA / residual updates
```

`BaselineStep` fields: `baseline`, `scale`, `z`, `tier` (`"seasonal"` |
`"rolling"`). `observed_at` must be UTC ISO-8601 with a `Z` suffix (same
as `iso_utc` / C2 `window_band_counts.observed_at`).

**C4 wiring (episode trigger):** each window:

1. `begin_window` — drain hold-and-backfill (no trusted-time accrual).
2. `preview` — read baseline / scale / `z` at pre-window trusted age.
3. `cusum_positive(S, step.z, …)`
4. `commit` — accrue trusted time, pin tier when entering an episode
   (`tier=step.tier`), apply EWMA updates unless frozen.

Step 4 freezes the trigger window once `S` rises above zero. `observe` is
a test convenience that runs 1→2→4 with the same `in_episode` throughout.

## What C3 is not

- **Not CUSUM.** `cusum_positive` stays in `crowd.py`; C4 wires both.
- **Not persistence.** C2 writes raw band counts; optional seed-from-rows
  is a future convenience, not this issue.
- **Not scan health.** P4-6 suppression is C4.
- **Not feedback gating.** L2 excludes labeled-benign episodes later.

## Tests (DoD)

- MAD floor at zero residual spread (delegates to `floored_mad`).
- Injected outlier windows: `z` spikes; baseline stays near the quiet
  mean after the episode ends.
- Seasonal vs rolling tier selection (clock trust + cold start).
- Episode freeze: EWMA **and residual history** unchanged while
  `in_episode=True`; tier pinned for the episode.
- Hold-and-backfill drains queued seasonal updates when trust flips
  and the detector is not in an episode.
