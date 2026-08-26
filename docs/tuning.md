# Tuning presence (the `[presence]` section)

Presence detection is the knob between "a wall of alerts" and "signal". In a
device-dense place — an apartment block, an office, anywhere phones and
wearables drift past — the out-of-the-box defaults will alert on *every*
unlabeled device that lingers, which is a firehose, not a sentinel. This guide
is the recipe for turning it down.

All of these live under `[presence]` in your config (see
`config.example.toml`) and take effect on the next daemon start.

## The mental model

The daemon scans in **windows**. One window is `scan.window + scan.pause`
seconds long — at the defaults (10 s scan + 5 s pause) a window is **15 s**.
Every `[presence]` count below is measured in windows, not seconds, so:

    seconds ≈ windows × (scan.window + scan.pause)

A device moves through a small state machine:

- **ABSENT → PRESENT** once it is heard **at or above the RSSI gate** for
  `appear_windows` windows in a row. Going PRESENT is what an unlabeled device
  needs to do to trigger an alert.
- **PRESENT → ABSENT** once it is missed (not heard, or heard *below* the gate)
  for `disappear_windows` windows in a row.
- A **cooldown** after leaving collapses a quick return into the same visit, so
  a device pacing at the edge of range does not re-alert on every lap.

Deciding *which* PRESENT devices actually message you (unknown vs. labeled) is a
separate layer — `/label` and `/ignore` in Telegram, or `/init` to walk every
currently-present unlabeled device one at a time. Tuning `[presence]`
changes *how many devices reach PRESENT at all*; labeling silences the specific
ones you have already accounted for. Use both.

## The knobs

| Key | Default | Raising it… |
| --- | --- | --- |
| `rssi_threshold` | `-80` | admits **only closer** devices (the big lever) |
| `appear_windows` | `3` | makes arrival **slower and calmer** |
| `disappear_windows` | `3` | makes a device linger longer before it counts as gone |
| `cooldown_windows` | `0` | suppresses re-alerts from a device flapping in and out |
| `prune_after_windows` | `4 × disappear` | only affects memory, not behaviour |

### `rssi_threshold` — the proximity gate (start here)

RSSI is the received signal strength in **dBm**. It is **negative**, and it
**rises toward 0 as a device gets closer**:

| Approx. RSSI | Rough distance |
| --- | --- |
| `-50` and up | very close — same desk / a metre or two |
| `-65` | same room |
| `-80` | same floor / through a wall (the default) |
| `-90` and below | far, or the noise floor — often a passer-by |

**Raising** the gate (e.g. `-80` → `-65`) means a device must be *physically
close* to the Pi to count as a hit — so distant phones in the hallway or the
next unit never reach PRESENT, and never alert. This single change is the
fastest way to quiet a dense site. It is bounded to `[-127, 0)`; a value of 0
or above is a sign typo that would silence the sentinel entirely (no real signal
reaches 0 dBm, so nothing would ever count), so the config rejects it at load
time.

Lowering the gate (e.g. `-80` → `-90`) does the opposite — you hear more of the
neighbourhood. That is the "collect everything" posture, not the alerting one.

### `appear_windows` / `disappear_windows` — debounce

These trade responsiveness for calm. Larger `appear_windows` means a device
must persist longer before it is "here", which further filters transient
passers-by (already largely handled by the gate) and delays a genuine alert.
Larger `disappear_windows` keeps a device marked PRESENT through brief signal
dropouts, so it does not churn ABSENT/PRESENT while sitting still at the edge of
range.

### `cooldown_windows` — one visit, not ten

By default (`0`) every return is a fresh visit and re-alerts. If a device keeps
crossing your range boundary — a neighbour's phone, a car in a driveway — set
`cooldown_windows` to a handful of windows so a return *within that many windows
of leaving* is treated as the same visit and stays silent. It only affects a
device that comes back quickly; to have any effect on an immediate return it
must be **larger than `appear_windows`** (reconfirming PRESENT already costs
that many windows).

### `prune_after_windows` — memory only

How long an ABSENT device is remembered before the tracker forgets it, bounding
RAM on a small Pi. Leave it unset unless you are tracking thousands of devices;
it changes nothing an operator would notice.

## A recipe for a dense site

Symptom: alerts on devices that are clearly not *in* your space — the firehose.

1. **Tighten the gate first.** Set `rssi_threshold = -65` (same-room). This
   alone removes most of the noise.
2. **Watch, don't guess.** Run for a bit and look at what is still reaching
   PRESENT (`journalctl -u blesentry.service | grep alerted`, and the RSSI in
   your scan captures). If close-but-uninteresting devices remain, that is a
   *labeling* problem, not a threshold one — `/ignore` them.
3. **Slow arrivals if needed.** If brief close passes still alert, raise
   `appear_windows` to `4`–`5`.
4. **Stop flapping.** If a single device re-alerts as it drifts in and out, set
   `cooldown_windows` to ~`8` (about two minutes at the default cadence).

Example — a deliberately quiet, proximity-only profile:

    [presence]
    rssi_threshold = -65    # same-room only
    appear_windows = 4      # ~1 min of persistence before it's "here"
    disappear_windows = 3
    cooldown_windows = 8    # a return within ~2 min is the same visit

## Daily summary (the `[summary]` section)

Once a day the daemon enqueues a digest covering devices seen, newly
created device rows, PRESENT/ABSENT transitions, and current outbox
depth (pending + failed). It is just another outbox message: a WAN
outage delays delivery; nothing is dropped. Device ids and operator
labels appear; addresses, fingerprints, and advertisement payloads do
not.

| Key | Default | Notes |
| --- | --- | --- |
| `enabled` | `true` | `false` skips the summary task entirely |
| `hour_utc` | `12` | Hour (0–23) at or after which today's digest may fire. UTC only. |

The last-sent marker lives in SQLite (`site_state`), so a restart after
today's digest does not send a second copy. The first digest after a
fresh database covers the previous 24 hours; later ones cover
`[last_sent, now)`. A daemon that was down for several days sends **one**
catch-up for the gap, not one row per missed calendar day.

To fire in the operator's morning, pick the UTC hour that matches; there
is no site-local timezone knob in v1 (no tz database on the 512 MB
target).

## Applying a change

`[presence]` and `[summary]` are read at start-up, so after editing the
config:

    sudo systemctl restart blesentry.service
    journalctl -u blesentry.service -f

There is no live reload — a sentinel that re-reads config mid-run could change
its alerting behaviour without a record of why, so a restart is deliberate.

## Reading the journal

The unit runs at INFO (`logging.basicConfig` in `blesentry run`). One
INFO line per scan window would fill the 64M persistent-journald cap
(`scripts/provision/install-service.sh.template`) in about a day, which
is too short for post-incident diagnosis. Per-cycle stats
are therefore **DEBUG**; INFO carries a **first-cycle liveness line**,
a **rollup every 60 cycles** (~15 min at the default 10 s window + 5 s
pause), and a leftover rollup when the loop **exits cleanly** (SIGTERM /
deploy restart). Unclean power loss does not run that leftover; the last
INFO rollup can lag by up to one interval (~15 min).

At INFO, `devices` in a rollup is the **sum of per-cycle unique
device counts**, not a distinct-id union across the window. `heard`
and `observations` are likewise sums.

Useful greps (no site identifiers; do not paste journal lines into
issues or PRs):

    journalctl --disk-usage
    journalctl -u blesentry.service | grep scanning
    journalctl -u blesentry.service | grep 'cycles '
    journalctl -u blesentry.service | grep alerted
    journalctl -u blesentry.service | grep 'daily summary'

Do not raise `SystemMaxUse` to buy retention — 64M is the SD-longevity
bound from the collector installer. Retention comes from quieter INFO.

## What tuning can't fix

Thresholds decide *presence*; they do not know *who* a device is. A close,
persistent device you simply do not care about (your own TV, a fixed sensor)
will keep reaching PRESENT no matter how you tune — that is correct. Silence it
by labeling: `/label <id> Living-room TV` to name it, or `/ignore <id>` to mute
it. To bulk-label everything that is currently present (first on-site pass,
or a catch-up after a busy day), `/init` in chat — or `blesentry init
--config config.local.toml` on the host — walks unlabeled PRESENT devices
one at a time. Reply with a name (no slash), `/skip`, `/ignore` (this device, no more
alerts), or `/done`. The session is snapshotted at start and time-boxed to
30 minutes of wall clock; after expiry the in-flight name is not applied and
`/init` (or `blesentry init`) starts a fresh snapshot. A daemon restart or a
switch between chat and CLI resumes the same cursor rather than building a
new list. `/init cancel` abandons it. On the CLI, EOF (Ctrl-D) pauses the
session for later resume rather than cancelling it. Drive one surface at a
time: if the CLI is waiting for a name, EOF-pause it before answering in
chat (and vice versa) — a name typed against a prompt that another surface
already consumed is discarded and the current device is re-prompted.
Presence tuning trims the population; labeling accounts for the residents.
