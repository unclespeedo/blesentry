# ADR-0001: OS Image — Raspberry Pi OS Lite Trixie arm64

- **Status:** Proposed
- **Date:** 2026-08-16
- **Deciders:** <human sign-off required — agents may draft, never accept>

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
2025-06, current stable as of 2026).

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

Bookworm ships glibc 2.36; Trixie ships glibc 2.38+. Both targets satisfy the
minimum. uv works identically on both — the decision is architecture, not
toolchain.

### arm64 vs armhf

The Pi 3 A+ has a 64-bit Cortex-A53 (ARMv8) CPU. Running 32-bit (armhf) on
64-bit hardware offers zero advantage and several costs:

- **No memory savings that matter.** Pointers are 4 bytes instead of 8, saving
  ~10–20MB in typical Python workloads. On 512MB this is negligible; the
  trade-off is worse ASLR entropy (32-bit address space = weak exploit
  mitigation).
- **Native 64-bit operations are faster** for integer and floating-point
  arithmetic — relevant for the fingerprint engine and RSSI processing.
- **The Python ecosystem targets aarch64.** bleak, aiosqlite, Pydantic V2, and
  uv are all well-tested on aarch64. armhf builds exist but receive less CI
  attention upstream.
- **RPi Foundation recommends 64-bit for Pi 3 and later.** The official RPi OS
  Lite 64-bit image is the primary target for their testing.

### BlueZ passive scan (D-Bus)

BlueZ versions shipped:

| OS | BlueZ version | Notes |
|---|---|---|
| Ubuntu 20.04 (current) | 5.53 | Baseline on this hardware |
| Bookworm (Debian) | 5.66 | Security-patched (5.66-1+deb12u2) |
| Bookworm (RPi patched) | 5.82 | RPi Foundation patches (5.82-1.1+rpt1) |
| Trixie (RPi patched) | 5.82+ | Latest Trixie release (2026-06-18) |

Passive BLE scanning works via D-Bus `SetDiscoveryFilter` on the
`org.bluez.Adapter1` interface. Key parameters:

- `Transport: "le"` — LE-only scan (no BR/EDR inquiry overhead).
- `DuplicateData: true` (default in code despite outdated docs) — emits
  `PropertiesChanged` on every advertisement update, including ManufacturerData
  and ServiceData changes. Essential for real-time RSSI tracking.
- RSSI threshold filtering available via the `RSSI` filter parameter.

**Known limitation:** BlueZ D-Bus does not expose scan interval/window control.
The `Scanner` seam documents raw HCI (bleson) as the escape hatch for this
(P0-3 risk notes, P4-3 spike). This limitation exists on all BlueZ versions
and does not differ between 5.66 and 5.82.

### Memory headroom (512MB)

Measured on current Ubuntu 20.04: 135MB used, 253MB available (407Mi total
before kernel reservations). RPi OS Lite is lighter — no snapd, no LXD, fewer
background services.

Estimated breakdown on RPi OS Lite Trixie arm64:

| Component | RAM |
|---|---|
| OS idle (kernel + systemd + sshd) | ~100–130MB |
| BlueZ + D-Bus | ~5–10MB |
| blesentry daemon (Python 3 + bleak + aiosqlite) | ~50–100MB |
| tailscaled (optional, access path only) | ~30–50MB |
| **Total** | **~185–290MB** |
| **Headroom** | **~220–325MB** |

This is comfortable. The 512MB constraint is real but not binding for this
workload.

### Trixie vs Bookworm

| | Bookworm (Debian 12) | Trixie (Debian 13) |
|---|---|---|
| Kernel | 6.1 (RPi: 6.6) | 6.12 (RPi: 6.18) |
| BlueZ | 5.66 / 5.82 (RPi) | 5.82+ (RPi) |
| Support until | ~2028 (LTS to 2029) | ~2030 (LTS to 2031) |
| Security hardening | Passwordless sudo default | Sudo disabled by default |
| Storage required (lite) | ~3.5GB | ~2.8GB |

Trixie is the better choice: longer support window, more modern kernel (better
BLE/hardware support), smaller footprint, and the sudo security improvement
aligns with a remote, internet-facing device. Bookworm remains the fallback if
Trixie introduces unforeseen compatibility issues with bleak or the project's
dependencies before the window closes.

## Consequences

**Easier:**
- Native 64-bit performance for Python, BLE processing, and fingerprint engine.
- Full uv toolchain support on the recommended target.
- Longer OS support window (Trixie through ~2030 vs Bookworm ~2028).
- Smaller attack surface (sudo disabled by default).
- Smaller disk footprint (~2.8GB vs ~3.5GB).

**Harder / trade-offs:**
- Trixie is newer (released June 2025) — less community battle-testing than
  Bookworm. If compatibility issues arise, the Bookworm fallback adds a
  re-flash step.
- The `DuplicateData` default (true in code, false in BlueZ docs) requires
  awareness when integrating bleak — the project must explicitly handle
  duplicate advertisement callbacks.

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
