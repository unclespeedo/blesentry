<!--
  SPDX-License-Identifier: MPL-2.0
  This Source Code Form is subject to the terms of the Mozilla Public
  License, v. 2.0. If a copy of the MPL was not distributed with this
  file, You can obtain one at https://mozilla.org/MPL/2.0/.
-->
# ADR-0005: Resolver seam — lifecycle, scoring, durable aliases

- **Status:** Accepted
- **Date:** 2026-08-26
- **Deciders:** Ryan Speed

## Context

ADR-0002 names three extension points (Scanner / Notifier / Storage).
The README architecture diagram has a fourth box — **Device resolver**
— that P1-7 (`DeviceResolver`) already implements. Its contract lived
only in docstrings: temporal coupling to the scan-cycle transaction,
a single-instance rule, and a pure scoring function. P2's presence
engine and unknown-device alerter key off the `device_id` this seam
returns. A mis-sequenced consumer (resolve outside the cycle
transaction, a fresh resolver every cycle, `commit` before COMMIT)
corrupts fusion memory silently — phantom ids after rollback, or
rotation joins that reset every window.

The resolver is **not** a plugin registry. There is one
implementation; thresholds are config (`[resolver]`), not a backend
selector. This ADR names the internal seam so later consumers cannot
re-derive the lifecycle from call-site folklore.

Fusion aliases (later fingerprints joined to an existing device)
originally lived only in process memory. After a restart, exact
founding keys recovered from `devices.fingerprint`; rotated keys had
to re-score against the seeded window. `docs/risks.md` names a
durable fusion audit trail as an impersonation mitigation — without
it, there is no record of which fingerprints were absorbed into
which identity. Persist-inside-`resolve` (#148) is the wiring that
closes that gap; see Decision below.

## Decision

### Named internal seam, not an extension point

`blesentry.resolver.DeviceResolver` is the Resolver seam. It is
**not** added to ADR-0002's config-selected plugin table. Third
parties do not ship alternate resolvers; they consume this one.

The frozen method surface:

```python
class DeviceResolver:
    def __init__(
        self,
        devices: DeviceRepository,
        *,
        min_score: float = 0.55,
        recent_window: int = 512,
    ) -> None: ...

    async def resolve(self, advertisement: Advertisement) -> int: ...
    def commit(self) -> None: ...
    def abort(self) -> None: ...
    async def seed(self) -> None: ...
```

`connection` (the underlying repository connection) is an
implementation convenience for the scan loop's ambient transaction,
not part of the frozen surface.

`fusion_score(candidate, other, *, address_type, other_address_type)
-> float` is a pure function: no I/O, no mutation, no repository.
Identity *policy* (weights, HAP veto, stable-address mismatch veto)
lives there; identity *state* lives on the instance. Changing the
method surface or the transaction-lifecycle rules below requires a
new ADR.

### Transaction-lifecycle contract

The scan loop (`run_cycle` / `run_loop`) is the reference consumer.
Every other caller must obey the same rules.

1. **One instance across cycles.** `run_loop` builds (or is given)
   a single `DeviceResolver` and passes it to every `run_cycle`.
   Constructing a new resolver per cycle drops the temporally-local
   rotation window and silently disables fusion across windows.
2. **`seed()` before the first cycle**, outside any cycle
   transaction. Seeding warms the exact-key cache and the recent
   window from `DeviceRepository.list_recent`. It is optional-cache
   warming: a tampered row is skipped, never fatal (fail-fast is for
   scanning, not for an optional cache).
3. **`resolve()` runs only inside the caller's cycle transaction.**
   Creates and address-touches are SQL writes; they must share the
   ambient unit of work (`storage.database.transaction`) with the
   observations (and presence events, alerts) of that window.
4. **`commit()` only after the transaction COMMITs.** Staged exact
   keys and window entries publish to committed-state maps. Calling
   `commit()` before COMMIT (or after a rollback) would leak phantom
   ids into fusion memory — the #84 lesson.
5. **`abort()` on any failure that rolls the transaction back.**
   Discard staged maps. `run_cycle` pairs this with `except
   BaseException:` so `CancelledError` cannot leave pending ids
   behind either.

`run_cycle` implements (3)–(5) as: `try: async with transaction:
resolve… except BaseException: abort(); raise` then `commit()` on
the success path, which runs only after the context manager has
COMMIT-ed. Callers that catch `run_cycle` errors and keep scanning
must still `abort()` (the loop already does) and must not reuse a
half-applied `PresenceTracker` (a different seam; see #112).

A caller that omits the resolver argument gets a fresh
`DeviceResolver` per `run_cycle` — documented as "rotation fusion
resets every call", not a supported production path.

### Scoring and the config floor

Weights (ACCEPTED-PROVISIONAL pending P0-11 walk-test tuning):

| Signal | Weight | Notes |
|---|---|---|
| Manufacturer payload equality | 0.5 | Strongest non-HAP join |
| Manufacturer company-id only | **0.3** | Must never fuse alone |
| Service UUID set equality | 0.25 | |
| Local-name equality | 0.25 | |
| Same address in-window | 0.6 | Two radios cannot share an address concurrently |
| HAP device-id match / mismatch | 1.0 / 0.0 | Near-authoritative; after the stable-address veto |
| Two known-stable differing addresses | 0.0 | Twin-product veto |

Default `min_score` is 0.55: full manufacturer-payload equality
alone is not enough; it needs name or service-set corroboration.

**Config floor (the #21 invariant):** `[resolver] min_score` MUST be
strictly greater than the company-only weight (0.3). At or below
0.3, every device that shares only a vendor company id collapses
into one identity — the rotation cloud becomes one row per vendor,
silently. `ResolverConfig` rejects `min_score <= 0.3` at load time.
The default 0.55 already satisfies the floor. Programmatic
`DeviceResolver(min_score=…)` used in tests is not floored. The
config-backed `blesentry run --config` path is the enforcement
point; flag mode (`--db` / `--site-id`) uses constructor defaults
that currently satisfy the floor but do not re-validate a custom
`DeviceResolver` injected by a caller.

Upper bound remains 2.0 — headroom for weight retuning, a
gross-typo net (catches `7`), not a promise that every accepted
value is reachable under the current weights (~1.6 non-HAP max).

### Durable-alias path (schema shipped; resolver persist wired)

Fusion aliases persist in `device_aliases`, deployed by migration
`0004_device_aliases.sql`. All SQL stays in `DeviceRepository`
(ADR-0002 storage seam). Columns:

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `site_id` | TEXT NOT NULL | Same site-partition rule as every table |
| `fingerprint` | TEXT NOT NULL | Canonical `fingerprint_key` JSON. `UNIQUE (site_id, fingerprint)` |
| `device_id` | INTEGER NOT NULL | FK → `devices(id)` |
| `created_at` / `updated_at` | TEXT NOT NULL | ISO-8601 UTC, same `strftime` default as `devices` |

Repository surface (the only legal access):

- `record_alias(*, fingerprint, device_id) -> int` — bind a fused
  key to a device. Same site only; unknown `device_id` raises.
  Re-bind of the same fingerprint to a **different** device raises
  (alias conflict — the audit trail must not silently rewrite
  identity). Same device is idempotent: return the existing row id
  with **no write** (do not bump `updated_at` — SD-longevity, the
  `touch_address` / #84 lesson).
- `get_by_alias(fingerprint) -> DeviceRow | None` — exact alias
  lookup, site-scoped. Returns the device's **founding-key** row;
  `row["fingerprint"]` is not the queried alias.
- `list_aliases(device_id) -> list[DeviceAliasRow]` — the fusion
  audit trail for one identity, oldest first.

`DeviceResolver.resolve` persists and consumes aliases (#148):

- After the founding-key exact match (`get_by_fingerprint`),
  `resolve()` consults `get_by_alias`. A hit is the same device;
  `touch_address` still runs when the advertisement carries an
  address. No second `record_alias` write (idempotent anyway).
- On a window fusion (`_best_match`), `resolve()` calls
  `record_alias` inside the ambient cycle transaction — the same
  unit of work as `touch_address` / observation inserts.
  `commit()` / `abort()` stay memory-only publish/discard.
- Founding-key creates (`upsert`) do **not** insert an alias row.
- `seed()` warms the exact-key cache from founding keys **and** each
  seeded device's aliases. Founding keys fill the recent-window
  budget first; aliases backfill leftover slots (newest first) so a
  later fingerprint can score against a rotated key after restart
  without letting one device's alias history evict every other
  identity.

Founding keys stay on `devices.fingerprint`. Alias rows are *later*
fused keys only — a fingerprint must not be both a founding key and
an alias. `DeviceRepository` enforces that cross-table rule on
`record_alias` and `upsert`.

## Consequences

- Presence, alerts, and bot commands can treat `device_id` as stable
  across a process lifetime without re-reading resolver internals.
- A second consumer (eval harness, force-scan, a future detector
  seam) has a written lifecycle; violating it is a bug, not a style
  choice.
- `device_aliases` is the durable fusion path and the impersonation
  audit trail `docs/risks.md` asked for. Schema-changing deploys
  still follow `docs/schema.md` (stop collector, deploy, start).
- This ADR is **Accepted** (human sign-off 2026-08-26). Changing the
  frozen surface is a new ADR, not an ADR-0002 amendment.
- ADR-0002 grows a pointer: Resolver is a named internal seam, not a
  fourth plugin.

## Future Considerations

- **Resolver persist-inside-`resolve` (done, #148).** `commit()`
  stays synchronous and post-COMMIT; alias rows are inserted from
  async `resolve()` inside the ambient cycle transaction.
  `resolve()` consults `get_by_alias` after the founding-key lookup;
  `seed()` warms from aliases. Changing `commit()` to async would
  itself be a new ADR.
- **Append-only fusion *event* log** if operators need history of
  re-binds rather than current binding. Cross-table uniqueness
  (founding key vs alias) is enforced in the repository (#148).
- **Contradiction detection** (the other impersonation mitigation
  `docs/risks.md` names) is still follow-up; the alias table is the
  audit trail, not a detector.
- Promoting Resolver to a `typing.Protocol` with a second
  implementation (e.g. a replay-only stub) would be a new ADR and
  is not required for v1.
