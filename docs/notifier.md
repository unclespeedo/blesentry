# Notifier backends

blesentry delivers alerts and reads operator commands through the
`Notifier` protocol (ADR-0002). The `[notifier]` TOML section selects
the backend; see `config.example.toml`.

| Backend | Outbound | Inbound commands | Secrets |
| --- | --- | --- | --- |
| `none` | Discarded (outbox marked delivered) | None | — |
| `telegram` | Bot API `sendMessage` (plain text) | Long-poll `getUpdates` | `bot_token` |
| `ntfy` | HTTP POST to alert topic | JSON poll on command topic | `token` (optional Bearer) |

## Telegram (v1 default platform)

Telegram is locked for the first deployment (ADR-0003): one private chat,
long-poll only (no webhook), single-operator auth via configured
`chat_id` **and** `user_id`. See ADR-0003 and `TelegramNotifier`.

## ntfy (P4-7)

[ntfy](https://ntfy.sh/) is a push service over HTTPS (or self-hosted
HTTP) — suitable for `ntfy.sh` or a private instance with
access-controlled topics. It fits the same outbound-only connectivity
model as Telegram (daemon initiates all connections).

**Public topics:** without a Bearer token, anyone who guesses a topic
name can publish commands. Use authenticated topics in production.

**Topics:** use two names — one for **alerts** (daemon publishes) and
one for **commands** (daemon subscribes). Splitting topics keeps alert
traffic out of the command stream and matches typical ACL layouts.

**Auth:** on a private server or paid tier, protect both topics with the
same Bearer token (`Authorization: Bearer …`). Anyone who can publish to
the command topic is treated as the operator (same practical trust as
holding the Telegram bot token). Configure `operator_user_id` for
`label_audit` actor strings (`ntfy:<id>` is not used — the numeric id
feeds the existing `tg:<user_id>` audit field for consistency).

**Commands:** subscribe with `poll=1` and `since=none` (then each
message `id`) on `{server}/{command_topic}/json`. Only `event=message`
payloads with a string `message` body become command text. The same
router as Telegram applies (`/status`, `/label`, `/init`, …) — publish
the command string to the command topic. Keepalive and other events are
ignored. The cursor advances only after the daemon finishes handling a
command (at-least-once redelivery on crash).

| Key | Required | Default |
| --- | --- | --- |
| `backend` | yes | — (`"ntfy"`) |
| `alert_topic` | yes | — |
| `command_topic` | yes | — |
| `server` | no | `https://ntfy.sh` |
| `token` | no | — (env: `BLESENTRY_NOTIFIER__TOKEN`) |
| `operator_user_id` | no | `1` |
| `poll_timeout` | no | `30.0` (seconds; client read timeout adds 10s headroom) |

**Alerts:** POST the plain-text body to `{server}/{alert_topic}` with
the same Authorization header when configured.

Round-trip on a live ntfy server is **HUMAN VERIFY** (no credentials in
CI).
