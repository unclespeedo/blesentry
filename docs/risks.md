# Scanner Feasibility Risks (P0-3)

Notes from the P0-3 spike: capture a real BLE advertisement corpus on the dev
Mac (CoreBluetooth) and research the BlueZ passive-scan path. These are the
"known caveats" the ROADMAP's risk register points at; the live capture is
committed under `tests/fixtures/`.

## macOS / CoreBluetooth (dev machine)

Observed and documented behaviour of the capture path on macOS:

- **Privacy permission required.** CoreBluetooth scanning needs the host app
  granted "Bluetooth" under System Settings → Privacy & Security. A headless
  process (e.g. a background agent) that lacks the grant fails immediately
  with `BleakBluetoothNotAvailableError` (`DENIED_BY_UNKNOWN`). Grant it in
  the privacy pane, not in code.
- **No real MAC address.** CoreBluetooth exposes the CBPeripheral *identifier*
  (a UUID), not the adapter MAC. bleak's `device.address` on macOS is that
  UUID. The `mac` field in corpus records is therefore *not* a stable hardware
  identity on macOS captures — fingerprinting must not depend on it (P1-7's
  fusion design already treats MAC as the weakest signal).
- **No passive scanning.** `scanning_mode="passive"` raises `BleakError` on
  macOS. Development captures run in CoreBluetooth's only mode (active).
  Passive-mode fidelity therefore has to be validated on the Pi (P1-3 DoD),
  not on the Mac.
- **Duplicate filtering is opaque.** CoreBluetooth delivers a callback when a
  device's advertisement data *changes*, not for every packet. Transient
  one-shot advertisements (e.g. a passing car) may be missed entirely or
  merged. This matches the ROADMAP risk that passive fidelity is the project's
  biggest technical unknown.

## BlueZ / Linux (production Pi)

Documented behaviour of bleak over D-Bus on Linux:

- **Passive mode requires bluetoothd `--experimental`.** Verified live
  on the Pi (BlueZ 5.82, Trixie, 2026-08-17): the AdvertisementMonitor
  D-Bus API is gated behind the experimental flag, and bleak raises
  `passive scanning on Linux requires BlueZ >= 5.56 with --experimental
  enabled` at scan start without it. Fix is a systemd drop-in
  (`bluetooth.service.d/experimental.conf` overriding ExecStart) —
  installed by `scripts/provision/firstrun.sh.template` and required
  by the P3-1 unit. With the flag enabled, a 12 s passive window heard
  20 devices using the scan CLI's default FLAGS `or_patterns`.
- **Passive mode requires `or_patterns`.** `scanning_mode="passive"` uses the
  `AdvertisementMonitor1` D-Bus interface and bleak *raises* if
  `bluez={"or_patterns": [...]}` is not supplied. Passive scanning also does
  not support service-UUID filtering (`service_uuids` is ignored there).
  Config for the Pi must ship a working pattern set (P1-3).
- **Duplicate filtering cannot be disabled through D-Bus.** bleak's
  `DuplicateData=False` discovery filter only affects BlueZ's D-Bus-side
  handling; the kernel `Filter Duplicates` bit of `LE Set Scan Enable` stays
  enabled for normal scanning (bluez#406, #647). Practical effect: you receive
  an update when advertisement data changes, not every repeat — short-lived
  devices are easy to miss. `hcitool lescan --duplicates` (raw HCI) is the
  only way to see every packet.
- **No scan interval/window control.** Scan parameters are kernel defaults;
  BlueZ does not expose interval/window via D-Bus, and bleak has no knob for
  it. Duty-cycle tuning for SD-card/power targets is therefore not possible on
  the BlueZ path (only via raw HCI).
- **Silent wedge.** Scans can return nothing while still "running" when the
  BlueZ/D-Bus stack wedges. There is no signal from the library — detection
  requires an external health check (P4-6 escalation ladder).

## Escape hatch: raw HCI (bleson as head start)

The documented fallback behind the `Scanner` seam (ADR-0002): talk to the
controller directly (e.g. bleson, or `hcitool`/mgmt sockets). Gains full
control — every packet, configurable scan interval/window, duplicates
disabled. Costs: root/privileged access, per-radio platform code, kernel
parameter wrangling, and a second normalization path. Feasibility spike is
P4-3, side-by-side capture-rate comparison against bleak on the Pi. Adoption
is a go/no-go on P4-3's numbers, never a silent switch.

## Implications

1. Corpus files recorded from macOS carry peripheral UUIDs in `mac`; the
   Pi-side P0-11 captures will carry real MACs. Tests must not assume one
   shape for both (P1-1 fixture parsing, P1-7 resolution).
2. Transient-advertisement loss is expected on both backends; the presence
   state machine (P2-1) must tolerate a *missed* 1–2 window signal by design,
   which its consecutive-window requirement already does.
3. Re-run `uv run scripts/capture_scan.py --duration 900 --out capture.json`
   to regenerate/extend the corpus; commit the result under
   `tests/fixtures/` (keep `adapter_id` distinct per backend).

## Fusion resolver limits (#19)

The resolver joins rotated addresses into device identities by
weighted signal scoring. Its deliberate conservatism and its known
gaps, from the adversarial review:

- **Impersonation inheritance.** Advertisements are unauthenticated.
  A spoofer replaying a known device's name/payload/uuids from a
  rotating-type address can be fused INTO that known identity,
  inheriting its status and polluting its history — where exact-key
  identity would have flagged a new device. Mitigation candidates
  (contradiction detection, fusion audit trail) are follow-up work;
  labeling and alert design (P2) must not treat fused identity as
  authenticated.
- **Provenance-null captures disable the twin veto.** The
  stable-address mismatch veto needs address_type on both sides;
  CoreBluetooth captures carry none, so twin products can fuse in
  Mac-corpus replays. Pi/BlueZ data carries provenance and is the
  deployment path.
- **Payload variance under-joins (partially mitigated by policy,
  2026-08-18).** Apple Continuity payload bytes vary, so weighted
  fusion of unnamed Apple devices rarely clears the threshold. Two
  approved strengtheners now apply: HAP (HomeKit type-0x06) stable
  device ids are parsed as authoritative identity (equal ids fuse at
  1.0; differing ids veto — covering provenance-null captures), and
  an exact address match within the resolver's temporally-local
  window fuses regardless of provenance (two radios cannot share an
  address concurrently). Non-HomeKit unnamed rotations still mostly
  stay unfused — conservative by design until walk-test tuning.
- **Restart amnesia (mitigated) and window bounds.** Exact keys
  recover from the database, and the resolver seeds its window from
  the newest stored devices at startup, so restart-spanning rotations
  mostly re-join. A device absent longer than the window still opens
  a new device row. An advertisement flood can
  flush the window (availability of fusion, not correctness — ties to
  the #85 flood posture).
