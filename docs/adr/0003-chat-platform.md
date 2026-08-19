<!--
  SPDX-License-Identifier: MPL-2.0
  This Source Code Form is subject to the terms of the Mozilla Public
  License, v. 2.0. If a copy of the MPL was not distributed with this
  file, You can obtain one at https://mozilla.org/MPL/2.0/.
-->
# ADR-0003: Chat platform — Telegram (v1)

- **Status:** Accepted
- **Date:** 2026-08-18
- **Deciders:** Ryan Speed

## Context

The sentinel alerts a single operator through a private chat channel
and accepts a small command set inbound (label, status, list,
force-scan, init). The deployment site sits behind carrier-grade NAT
and a firewall with no inbound exposure; the notifier must work
purely over outbound connections. The Notifier seam (ADR-0002) keeps
the platform swappable.

## Decision

**Telegram** is the v1 chat platform.

- One bot (token in local config, never committed) and one private
  chat.
- **Long-poll `getUpdates` only — no webhook.** Long polling is an
  outbound HTTPS request, so it functions behind CGNAT/firewalls with
  zero inbound surface. A webhook would require a public endpoint the
  deployment explicitly must not have.
- **Security posture:** transport security (Telegram's TLS) plus a
  private chat is sufficient for v1. Single-operator authorization
  rule, stated verbatim for P2-5/P2-8 to implement:

  > An inbound message is authorized if and only if the configured
  > `chat_id` AND the configured `user_id` both match the message's
  > origin. Either mismatch → the message is ignored and logged.

- Outbound messages flow exclusively through the outbox
  (P2-3/P2-4): written before any delivery attempt, drained with
  backoff — never fire-and-forget.

## Consequences

- Phase 2 (#23–#30) is unblocked: TelegramNotifier implements the
  Notifier protocol; MockNotifier serves CI.
- Discord and ntfy remain future Notifier adapters (P4-7) behind the
  same seam — a config edit, not a rewrite.
- Bot token and ids live in local config (`config.local.toml`,
  gitignored) per SECURITY.md; agents never handle the live token
  (`needs:secrets` labeling applies to live verification).
