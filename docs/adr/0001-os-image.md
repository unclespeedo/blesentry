<!--
  SPDX-License-Identifier: MPL-2.0
  This Source Code Form is subject to the terms of the Mozilla Public
  License, v. 2.0. If a copy of the MPL was not distributed with this
  file, You can obtain one at https://mozilla.org/MPL/2.0/.
-->
# ADR-0001: OS Image — Raspberry Pi OS Lite Trixie arm64

- **Status:** Accepted
- **Date:** 2026-08-16 (accepted 2026-08-17)
- **Deciders:** Ryan Speed

## Context

blesentry's first deployment target is a Raspberry Pi 3 Model A+ (Cortex-A53,
512MB RAM, WiFi-only, microSD boot). The OS image must:

1. Support `uv` (Python toolchain) on the target architecture.
2. Ship BlueZ with functional passive BLE scanning via D-Bus.
3. Run within 512MB RAM with headroom for the daemon, BLE stack, and optional
   tailscaled.
4. Be supported long enough to survive the project's operational life.

Two architectures are candidates: **arm64** (64-bit, native to Cortex-A53) and
**armhf** (32-bit ARMv7 hard-float). Two Debian release tracks are candidates:
**Bookworm** (Debian 12, current stable) and **Trixie** (Debian 13, released
2025-08-09, current stable as of 2026).

## Decision

**Raspberry Pi OS Lite, Trixie (Debian 13), arm64.**

Fallback: Bookworm arm64 if Trixie compatibility issues arise before the
physical-access window closes (2026-08-30).

armhf (32-bit) is rejected.

## Rationale

### uv on arm64 and armv7

uv publishes Tier 2 binaries for both targets:

| Target | Binary | glibc requirement |
|---|---|---|
| `aarch64-unknown-linux-gnu` | `uv-aarch64-unknown-linux-gnu.tar.gz` | 2.28+ |
| `armv7-unknown-linux-gnueabihf` | `uv-armv7-unknown-linux-gnueabihf.tar.gz` | 2.17+ |

Bookworm ships glibc 2.36; Trixie ships glibc 2.41. Both targets satisfy the
minimum. Tier 2 means "guaranteed to build" — the uv test suite is not run on
either target, so runtime stability is not formally guaranteed. The decision is
architecture, not toolchain; runtime verification is part of this spike (issue
#1).

### arm64 vs armhf

The Pi 3 A+ has a 64-bit Cortex-A53 (ARMv8) CPU. Running 32-bit (armhf) on
64-bit hardware is possible but disfavored for several reasons:

- **Dependency-wheel availability.** aarch64 wheels are the primary CI target
  for bleak, aiosqlite, Pydantic V2, and uv. armhf wheels exist but receive
  less upstream testing attention.
- **Operational consistency.** arm64 is the RPi Foundation's recommended
  architecture for Pi 3 and later, and the official RPi OS Lite 64-bit image
  is their primary testing target.
- **Security posture.** 64-bit ASLR uses the full address space; 32-bit ASLR is
  weaker. On a remote, internet-facing device, this matters.
- **Memory trade-off.** 32-bit pointers save ~10–20MB in typical Python
  workloads (4-byte vs 8-byte pointers). On a device with ~407MiB visible to
  Linux, this is non-trivial but not decisive — the dependency and consistency
  factors dominate.

### BlueZ passive scan (D-Bus)

BlueZ versions shipped:

| OS | BlueZ version | Notes |
|---|---|---|
| Ubuntu 20.04 (current) | 5.53 | Baseline on this hardware |
| Bookworm (Debian) | 5.66 | Security-patched (5.66-1+deb12u2) |
| Bookworm (RPi patched) | 5.82 | RPi Foundation patches (5.82-1.1+rpt1) |
| Trixie (RPi patched) | 5.82+ | RPi OS Trixie image available 2026-06-18 |

BLE scanning uses D-Bus `SetDiscoveryFilter` on the `org.bluez.Adapter1`
interface. Key parameters:

- `Transport: "le"` — LE-only scan (no BR/EDR inquiry overhead). Note: this
  selects LE *transport*, not passive scanning mode. Bleak defaults to *active*
  scanning on BlueZ; passive mode requires advertisement-monitor patterns
  (see bleak docs).
- `DuplicateData` — BlueZ defaults this to true (unfiltered), but Bleak's
  `BleakScannerBlueZDBus` explicitly sets it to false. To receive continuous
  advertisement updates including ManufacturerData and ServiceData changes
  (essential for real-time RSSI tracking), the scanner filter must override
  this with an explicit `DuplicateData: true`.
- RSSI threshold filtering available via the `RSSI` filter parameter.

**Known limitation:** BlueZ D-Bus does not expose scan interval/window control.
The `Scanner` seam documents raw HCI (bleson) as the escape hatch for this
(P0-3 risk notes, P4-3 spike). This limitation exists on all BlueZ versions
and does not differ between 5.66 and 5.82.

### Memory headroom (~407MiB visible)

The Pi 3 A+ has 512MB physical RAM, but only ~407MiB is visible to Linux after
GPU/kernel reservations (confirmed on current Ubuntu 20.04: 135MB used, 253MB
available). RPi OS Lite is lighter — no snapd, no LXD, fewer background
services.

Estimated breakdown on RPi OS Lite Trixie arm64:

| Component | RAM |
|---|---|
| OS idle (kernel + systemd + sshd) | ~100–130MB |
| BlueZ + D-Bus | ~5–10MB |
| blesentry daemon (Python 3 + bleak + aiosqlite) | ~50–100MB |
| tailscaled (optional, access path only) | ~30–50MB |
| **Total** | **~185–290MB** |
| **Headroom on ~407MiB** | **~117–222MiB** |

This is tight but workable. The constraint is real and should be validated by
measurement on the selected image before committing to the deployment posture.

### Trixie vs Bookworm

| | Bookworm (Debian 12) | Trixie (Debian 13) |
|---|---|---|
| Kernel | 6.1 (RPi: 6.6) | 6.12 (RPi: 6.18) |
| BlueZ | 5.66 / 5.82 (RPi) | 5.82+ (RPi) |
| LTS support until | June 2028 | June 2030 |
| Passwordless sudo | Enabled by default | Enabled by default (RPi OS 6.2+ disables for fresh installs) |
| Storage required (lite) | ~3.5GB | ~2.8GB |

Trixie is the better choice: longer support window (LTS through June 2030 vs
June 2028), more modern kernel (better BLE/hardware support), smaller
footprint, and RPi OS 6.2 disables passwordless sudo by default for fresh
installations — aligning with a remote, internet-facing device. Bookworm
remains the fallback if Trixie introduces unforeseen compatibility issues with
bleak or the project's dependencies before the window closes.

## Consequences

**Easier:**
- Native 64-bit performance for Python, BLE processing, and fingerprint engine.
- Full uv toolchain support on the recommended target.
- Longer OS support window (Trixie LTS through June 2030 vs Bookworm June 2028).
- Smaller attack surface (RPi OS 6.2+ disables passwordless sudo for fresh
  installs).
- Smaller disk footprint (~2.8GB vs ~3.5GB).

**Harder / trade-offs:**
- Trixie is newer (released August 2025) — less community battle-testing than
  Bookworm. If compatibility issues arise, the Bookworm fallback adds a
  re-flash step.
- Bleak's BlueZ backend sets `DuplicateData` to false — the project must
  override this to true in the scanner filter to receive continuous
  advertisement updates. Failure to do so will silently drop RSSI changes
  between scan windows.

**Locked in:**
- arm64 target for all uv, Python, and binary dependencies going forward.
- BlueZ D-Bus as the scanner interface (raw HCI is the documented escape hatch
  but adds complexity).
- Raspberry Pi OS Lite as the base image (no Docker, per project constraints).

**Revisit triggers:**
- If bleak or a critical dependency fails to build or run on Trixie arm64 before
  the window closes → fall back to Bookworm arm64.
- If raw HCI becomes necessary (P4-3 spike) and BlueZ version matters for HCI
  access → reassess Trixie's BlueZ 5.82+ HCI support at that time.
