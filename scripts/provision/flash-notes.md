# Pi flash & headless provisioning procedure (P0-7)

Repeatable Mac → microSD procedure for a blesentry node. Written
generically so a second site/board reuses it. Everything here was
validated live on 2026-08-17 against a Pi 3 A+ and the Trixie
2026-06-18 image; the pitfalls below are all things that actually
happened.

## 1. Downloads (~530 MB total)

- **OS image** per ADR-0001: Raspberry Pi OS **Lite**, **Trixie**,
  **arm64**, from raspberrypi.com/software/operating-systems (501 MB,
  resumable):

      curl -L -C - -O https://downloads.raspberrypi.com/raspios_lite_arm64/images/<release-dir>/<image>.img.xz

- **Verify the checksum** (append `.sha256` to the same URL):

      curl -sL <image-url>.sha256 | shasum -a 256 -c

- **Raspberry Pi Imager** (~25 MB): `brew install --cask raspberry-pi-imager`

## 2. Flash

Imager → Choose Device → Choose OS (*Use custom* → the verified
`.img.xz`; no need to decompress) → Choose Storage → write.

> **Pitfall:** Imager 2.x only offers its OS-customisation dialog for
> images picked from its own catalog. For *Use custom* images it
> silently writes a **vanilla** card. Do NOT rely on the dialog;
> provision via `firstrun.sh` (next step) instead.

> **Pitfall:** the Trixie 2026-06-18 image does **not** process
> `custom.toml` — `/usr/lib/raspberrypi-sys-mods/` ships empty. A
> `custom.toml` placed on the boot partition is ignored forever. The
> `firstrun.sh` mechanism below is image-agnostic and works.

## 3. Headless provisioning (firstrun.sh)

With the freshly written card mounted (`/Volumes/bootfs`):

1. Copy `firstrun.sh.template` (this directory) to
   `/Volumes/bootfs/firstrun.sh` and fill in every `{{PLACEHOLDER}}`:
   hostname, username, password hash (`openssl passwd -6 '<pw>'`),
   SSH public key, WiFi SSID/PSK/country, timezone.
2. Append to the single line in `/Volumes/bootfs/cmdline.txt`
   (leading space, no newline before it):

       systemd.run=/boot/firmware/firstrun.sh systemd.run_success_action=reboot systemd.unit=kernel-command-line.target

3. The template also installs the **bluetoothd `--experimental`
   drop-in** — required for passive BLE scanning (see
   `docs/risks.md`); without it every `blesentry scan` fails at
   startup.

## 4. Pre-boot verification (do not skip)

Ten seconds here beats a blind flash-boot-debug cycle:

    bash -n /Volumes/bootfs/firstrun.sh          # syntax
    grep -c 'ssh-' /Volumes/bootfs/firstrun.sh   # key present (>=1)
    grep systemd.run /Volumes/bootfs/cmdline.txt # hook present
    cat /Volumes/bootfs/cmdline.txt | wc -l      # exactly 1 line

Then the **round-trip test** (counterfeit-card insurance for an
unattended deployment): `sync`, eject, physically remove, reinsert,
and confirm the files are still there (`shasum` them). A card that
loses a verified write across a power cycle is disqualified.

> **Pitfall:** if `diskutil list` shows the card with *no partitions*
> after reinsertion, reseat it — a flaky reader contact produced
> exactly this symptom and mimicked a dead card.

## 5. First boot

Insert, power, and leave it alone. Sequence: resize + reboot, then
`firstrun.sh` + self-reboot. WiFi appears **1–5 minutes** after
power-on. Solid red + flickering green LEDs = healthy.

Find it and verify (this is the P0-7 DoD):

    ping <hostname>.local
    ssh <user>@<hostname>.local            # must succeed on the key
    ssh -o PubkeyAuthentication=no <user>@<hostname>.local
    # must print: Permission denied (publickey)  <- key-only proof

## 6. If it never joins the network

`firstrun.sh` writes `/boot/firmware/blesentry-debug.log` — power
off, mount the card on the Mac, and read it: it contains `rfkill`
state, the WiFi scan the Pi actually saw, NetworkManager's journal,
and `ip addr`. No serial console needed.

Also check: is `firstrun.sh` gone and `cmdline.txt` restored? If
`firstrun.sh` is still present, the hook never ran — recheck step 3.2.

## 7. Post-boot toolchain (until the P0-9 deploy script exists)

    ssh <user>@<hostname>.local
    curl -LsSf https://astral.sh/uv/install.sh | sh
    curl -sL https://github.com/unclespeedo/blesentry/archive/refs/heads/main.tar.gz | tar xz
    cd blesentry-main && ~/.local/bin/uv sync
    ~/.local/bin/uv run blesentry scan --duration 10   # smoke test

Validated result 2026-08-17: 20 devices heard in a 12 s passive
window on BlueZ 5.82 with the CLI's default `or_patterns`.

## 8. Collector service (do this once per provisioned card)

Installs the boot-enabled collector enforcing the operational
invariants: scan on every boot with no network dependency, restart
on failure indefinitely, deploy-restart authorization (so the latest
deployed code is always the running code), persistent bounded
journals (`Storage=persistent`, `SystemMaxUse=64M`). The daemon logs
at INFO; per-cycle scan stats are DEBUG so they do not consume that
cap. INFO is a first-cycle liveness line, a rollup every 60 cycles
(~15 min at `--window 10 --pause 5`), and a leftover rollup on
graceful shutdown (SIGTERM / deploy restart). Unclean power loss
skips that leftover — at most one rollup interval without a tail
line. See `docs/tuning.md` ("Reading the journal"). Do not raise
`SystemMaxUse` without revisiting SD wear.

1. Copy `install-service.sh.template` (this directory) and fill in
   every `{{PLACEHOLDER}}` (username, site id); save the result as
   `/Volumes/bootfs/install-service.sh`.
2. Append to the single line in `/Volumes/bootfs/cmdline.txt`
   (leading space, no newline before it — same hook as step 3;
   it fires ONLY under `kernel-command-line.target`):

       systemd.run=/boot/firmware/install-service.sh systemd.run_success_action=reboot systemd.unit=kernel-command-line.target

3. Pre-boot verification (do not skip):

       bash -n /Volumes/bootfs/install-service.sh
       grep -c '{{' /Volumes/bootfs/install-service.sh   # exactly 0
       grep -c systemd.run /Volumes/bootfs/cmdline.txt   # exactly 1
       wc -l /Volumes/bootfs/cmdline.txt                 # exactly 1 line

4. Eject, boot. Expect two boots (install -> self-reboot -> service
   running). Verify by reading `install-service.log` on the boot
   partition: it ends with `install complete` and the script has
   removed itself. On failure the script keeps itself and the log,
   removes the boot hook (no retry loop), and the log names the
   failed step.
