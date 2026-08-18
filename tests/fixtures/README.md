# Fixture corpora — sanitization protocol

Corpora in this directory are captured at a real site. This repo is
public. Every corpus MUST pass this protocol before it is committed
(AGENTS.md Hard Prohibitions). Raw captures never leave the capture
machine; the committed file is the sanitized derivative.

## Preserve verbatim (tests and #19 fusion depend on these)

- Company IDs and TLV type/length structure in manufacturer data
- Service UUID lists; flags and status bytes; tx_power; RSSI values
- Address-type semantics: the top two bits of the first MAC octet
  (RPA `01` / static-random `11` / public) must survive mapping —
  the resolver scores MAC trust by rotation class
- Inter-record timing (deltas between timestamps)
- Per-device consistency: the same physical device maps to the same
  pseudonymous identifiers across all records in the corpus,
  or MAC-rotation and fusion tests are meaningless

## Scramble (deterministic keyed mapping; key stays local)

- MAC/NIC bytes: keyed pseudonym; keep the OUI only for public
  addresses (vendor realism — the company ID discloses vendor
  anyway); preserve the address-type bits above
- Embedded IPv4s and ports inside payloads (e.g. Apple AirPlay
  type-0x09 TLVs): remap to 192.0.2.x / port 0, length-preserving
- Rotating keys, auth tags, nonces (e.g. Find My public keys,
  Nearby auth tags): random same-length bytes
- Serials or MACs embedded in service data (e.g. Xiaomi MiBeacon):
  same keyed mapping as the address field
- local_name: keep vendor/model substrings, drop personal tokens
  ("Ryan's MacBook Pro" -> "MacBook Pro")
- Absolute timestamps: shift the whole corpus by one random offset
  (deltas preserved; wall-clock occupancy signal destroyed)

## Ground-truth labeled sessions (P0-11)

Labels are the point of those captures and cannot be anonymized
without destroying them: the raw labeled sessions stay in private
storage; only the pseudonymized derivative (labels reduced to
`device-A`, `walker-1`) is committed. Walk/drive captures record
third parties' devices — the protocol applies to every record, not
just ours.

## Why this matters here

This project's alert condition is "unknown device appears." A
published raw corpus of the deployment site is a spoofing oracle: it
hands an adversary the exact fingerprints needed to advertise as a
known device. Sanitized corpora keep tests byte-realistic without
mapping back to the site.
