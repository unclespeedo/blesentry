<!--
  SPDX-License-Identifier: MPL-2.0
  This Source Code Form is subject to the terms of the Mozilla Public
  License, v. 2.0. If a copy of the MPL was not distributed with this
  file, You can obtain one at https://mozilla.org/MPL/2.0/.
-->
# Site inventory and fixed-gear labeling

Presence tuning (`docs/tuning.md`) decides *how close* a device must
be before it counts as here. Inventory labeling decides *which*
persistent devices you already know about so they stop driving novelty
alerts and detector false positives. This guide is the operator recipe
for the first on-site pass and for remote catch-up when you cannot
SSH to the Pi but can read its database locally.

Keep site-specific facts (MACs, addresses, raw advertisement payloads,
room names tied to a deployment) in gitignored local notes — not in
public issues or PRs. See `SECURITY.md` and `CONTRIBUTING.md`.

## Two different questions

| Question | Tool |
| --- | --- |
| "Is something *close*?" | `[presence] rssi_threshold`, `appear_windows` |
| "Is this *our* gear?" | `/label`, `/ignore`, `/init`, familiar set (F6) |
| "Is someone *approaching*?" | `[detection] backend = "approach"` |
| "Is unknown gear *on us*?" | `[detection] backend = "inside"` |

Trim the population first, then name the residents. Skipping inventory
work at a device-dense site leaves dozens of unlabeled fixtures
competing with genuine visitors.

## One physical device, many `device_id` rows

blesentry keys observations to a resolved **`device_id`**, not to a
BLE address alone. The fingerprint is address + local name +
manufacturer data + service UUIDs (see README architecture).

When any of those fields change, the resolver may mint a **new**
`device_id` even though the radio is the same fixture on the wall.
That is normal for:

- **Phones and wearables** — rotating private addresses and changing
  payloads (expected transient noise).
- **Some HomeKit accessories** — manufacturer-data variants on a
  stable accessory identity; blesentry may treat each variant as a
  separate founding fingerprint until aliasing catches up (ADR-0005).

**Do not equate `COUNT(DISTINCT device_id)` with physical device
count.** A cabin with six installed fixtures can easily produce hundreds
of historical rows after weeks of scanning.

### Label anchors, not every shard

Pick one strong, recent `device_id` per physical item and give it a
human label. Sibling shards with the same label are fine for operator
chat and digests; detector familiar-set logic treats labeled ids as
known class members (`docs/familiar.md`).

When two shards share a label but only one is active, the label still
marks the *class* — do not assume silence on an old id means the
hardware left.

## Device classes and what to expect on BLE

Passive scanning only sees devices that **advertise**. Many installed
products spend most of their time on Wi-Fi or Thread and appear on BLE
rarely, briefly, or not at all.

| Class | Typical examples | BLE presence |
| --- | --- | --- |
| Battery door/window & motion (HomeKit / Matter MTD) | Contact sensors, PIR | Event bursts + slow idle cadence; may go quiet while a hub is connected; Thread-primary units use BLE mainly as fallback |
| Mains Thread router (FTD) | Smart plug, in-wall switch | Steady advertiser when powered; relay toggles **load**, not the plug's own radio |
| Unnamed Apple manufacturer data | HomePod, AirPods, some HomeKit gear | No `local_name`; cluster by stable **manufacturer-data prefix** and RSSI tier |
| Named sport / beacon gear | Bike lights, heart-rate straps | Often advertise a readable local name |
| Security cameras & base stations | Many brands | Usually **Wi-Fi / proprietary P2P**; BLE only during setup or rare service modes |
| Routers, satellite terminals | AP, dish | Usually **not** on BLE for presence |
| AP location beacons | Some enterprise Wi-Fi | Only if BLE beaconing is enabled and in range |

If a device never appears in `/list` or the database despite being
physically present, absence from BLE is a plausible explanation — not
proof the scanner is broken.

### Battery contact sensors (Eve Door & Window and peers)

These are the most common source of "my labeled Entry went silent"
confusion:

- **Closed / idle** does not necessarily mean "not there." Many units
  advertise on a **slow schedule** (vendor privacy notices often cite
  minutes-scale idle) and on **state changes** (open/close).
- **Thread-first** installs may rarely fall back to BLE when a border
  router is healthy.
- **HomeKit Accessory Protocol** accessories can **stop advertising
  while a controller session is active** (phone or home hub).

A sensor that shows thousands of observations over days, then zero on
one `device_id`, may have **rotated fingerprint** onto a new row — check
sibling shards with similar manufacturer-data prefixes and RSSI before
assuming battery death.

### Unnamed Apple clusters

HomePod-class and AirPods-class gear often advertises **without a
local name**. In SQL or local analysis, group by the hex prefix of
`manufacturer_data[0][1]` rather than chasing every new `device_id`.

Expect **RSSI tiering** to correlate with layout: fixtures nearest the
Pi tend to sit ~5–15 dB stronger than siblings across a room, though
multipath indoors compresses that spread. Use RSSI to **prioritize
label candidates**, not to measure metres.

### Smart plugs (Eve Energy and peers)

A mains smart plug is a **relay on the outlet load**. Toggling it
remotely:

- **Does** power-cycle whatever is plugged **downstream** (lamp,
  charger, some mains-powered gear).
- **Does not** power-cycle a **battery sensor** on the wall — those
  are not on the switched circuit.
- **Does not** unpower the plug itself — the Thread/BLE controller
  stays on (<1 W standby) while the relay is open.

Remote plug cycling is a poor test for "which contact sensor is which."
It **can** reboot a **mains-powered** neighbor on that outlet (e.g. a
home hub on the same power strip) and indirectly change Thread/BLE
behavior — that is a second-order effect, not a sensor reboot.

## Recommended workflow

### 1. First on-site pass

1. Tighten `[presence]` per `docs/tuning.md` (start at
   `rssi_threshold = -65` in dense RF).
2. Run `/init` or `blesentry init` and name everything that is
   **PRESENT** and clearly yours; `/ignore` transient neighbors you
   cannot avoid hearing.
3. Note **physical layout** in private local notes (which sensor is
   nearest the Pi) — blesentry does not store room geometry.

### 2. Remote catch-up (no one on site)

Pick a **human-departure cutoff** (last known time the site was
occupied). Devices with sustained observations **after** that cutoff
are candidate fixed gear; spiky, weak, or nameless rows that only
appeared on occupied days are often phones or passers-by.

On a **read-only copy** of the database (never the live WAL file the
daemon writes):

- Rank post-cutoff devices by **observation count** and **average
  RSSI**.
- Compare **labeled groups** (`GROUP BY label`) before relabeling
  shards.
- For unnamed Apple traffic, aggregate by **manufacturer-data prefix**.

Do not publish query output that contains addresses, fingerprints, or
payloads.

### 3. Verify before relabeling

When two labeled rows share nearly identical fingerprints but different
`device_id`s, check:

- Same or alternating BLE address with **one-byte manufacturer-data
  diffs** → likely one physical accessory; prefer **one label** on
  the active shard.
- Distinct address **and** distinct manufacturer-data family → likely
  **two** physical devices (e.g. entry vs deck).

Relabeling is an operator action (`/label`, SQL update on `devices`,
or `/init`); the resolver does not infer room names.

## What remote testing can and cannot do

| Action | Useful for inventory? |
| --- | --- |
| Raise/lower `rssi_threshold` | Yes — reduces noise class |
| `/label`, `/ignore`, `/init` | Yes — primary inventory tool |
| Toggle smart-plug relay | Only for **mains load** on that outlet |
| Remote "restart accessory" in vendor app | Sometimes — hub/sensor reconnect behavior varies |
| Power-cycle battery sensor remotely | **No** — requires physical battery access |
| Door open/close (automations) | Yes — triggers sensor event adverts if reachable |

## Pitfalls

1. **Labeling the loudest shard wrong** — nearest-to-Pi RSSI on a
   contact sensor may attach to an Entry id while a Deck id looks
   weaker on a different fingerprint variant.
2. **Expecting one row per fixture** — plan for multiple `device_id`s
   per installed item until alias fusion improves.
3. **Counting Meraki / cameras / dish gear in BLE inventory** — many
   never advertise; absence is inconclusive.
4. **Using plug toggles to identify battery sensors** — electrically
   impossible; wastes time remotely.
5. **Posting inventory SQL to GitHub** — counts and redacted labels
   only in public artifacts; full fidelity stays local.

## Related docs

- `docs/tuning.md` — `[presence]` and daily digest
- `docs/familiar.md` — auto-learned resident baseline (F6)
- `docs/inside.md` — adjacent-to-Pi detector and own-gear exclusion
- `docs/adr/0005-resolver-seam.md` — fingerprint fusion and aliases
