<!--
SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
SPDX-FileCopyrightText: Copyright (C) 2026 OCUDU contributors
SPDX-License-Identifier: BSD-3-Clause-Open-MPI
-->

# O-RU Mplane service

With `--ru_supervise` the O1 adapter owns a long-lived, supervised Mplane
relationship with an O-RU: it keeps the o-ran-supervision watchdog fed for
the process lifetime, reconnects on any failure, and reports the session's
lifecycle through the adapter's shared state, its log and its alarm registry
(and northbound to VES through the existing alarm notifier). The O-RU becomes a
managed element rather than a one-shot edit-config target; provisioning
itself stays with the client CLI ([mplane-client.md](mplane-client.md)) and
the `--ru_forward` path.

```
                        ┌─────────────────── o1_adapter ───────────────────┐
   O-RU  ◄──NETCONF──►  │  MplaneSession ──► AlarmManager ──► VES          │
  (server)              │  (2 sessions,   ──► AppState.session_state       │
                        │   watchdog,          + INFO log per transition   │
                        │   state machine)──► RuConfig (client library)    │
                        └──────────────────────────────────────────────────┘
```

## The session (`mplane_session.py`)

Two NETCONF sessions per O-RU — a **command session** (RPCs, edits, reads)
and a **notification session** (`create-subscription` puts a session into
notification mode, so the roles cannot share one without `:interleave`).
Supervision is the exception to that split: o-ran-supervision watchdog
timers are per NETCONF session, held by the session that subscribed, so
`supervision-watchdog-reset` is dispatched on the notification session
(`RuConfig(..., supervision_manager=)`). Lifecycle:

```
DISCONNECTED → CONNECTING → SUPERVISED ⇄ DEGRADED
      ▲ ______________________________________|   (reconnect, retry backoff)
```

- **SUPERVISED**: both sessions up, subscription active, watchdog fed. One
  `supervision-watchdog-reset` is sent immediately after subscribing (the
  supervision budget starts when the O-RU enters supervised mode), then
  resets are notification-driven — a blind timer would mask a real O-RU
  failure. Only an *accepted* reset enters SUPERVISED: an rpc-error reply
  means the O-RU is alive but did not arm its watchdog, so the session holds
  the connection without claiming supervision. The reply's `next-update-at`
  may extend the locally computed starvation deadline (the O-RU may lawfully
  keep its own timers) but never shortens it — the leaf is stamped by the
  O-RU's clock, which need not agree with the host's — and only up to twice
  the local budget: a promise beyond that is treated as an O-RU clock running
  ahead (one WARNING per connect cycle) and the local budget applies.
- **DEGRADED**: no supervision-notification within
  notification-interval + guard. Raises alarm **1004
  RU_SUPERVISION_FAILURE**, attempts a recovery reset, keeps listening.
  DEGRADED exits only when a watchdog reset is *accepted* again — a
  supervision-notification alone does not restore SUPERVISED.
- **Error taxonomy**: an rpc-error reply is *alive-but-rejected* — counted,
  never fatal. Everything else in the ncclient exception tree — including
  reply timeouts and missing capabilities, which are `NCClientError`
  siblings of `OperationError`, not children — recycles the session through
  teardown + reconnect. A connect attempt can also fail below ncclient: a
  peer that closes the connection during the SSH handshake surfaces as
  paramiko's bare `EOFError` (ncclient wraps only `SSHException`), as an
  O-RU resetting mid call-home does; it is a connect failure like a refused
  socket — logged, 1003, retried. No failure mode escapes into the
  orchestrator.
  Teardown is bounded: the `close-session` RPC gets a 5 s deadline and a
  transport still up afterwards is closed directly, so a dead O-RU cannot
  hold the reconnect loop for the RPC timeout per session.
- **Idle command session**: NETCONF servers reap idle sessions that hold no
  subscription (netopeer2 defaults to 180 s), so the command session issues
  a cheap strict sync-status read every interval/2 (clamped under the reap
  age); its failure recycles the cycle, which is the correct discovery of a
  dead command session.
- **Alarms**: **1003 RU_NETCONF_CONNECTION_LOSS** on connect failure or
  session loss, cleared as soon as both sessions are up and the subscription
  is active — before the first watchdog reset, so a reconnect clears it even
  when the O-RU keeps rejecting resets (1004 tracks supervision separately).
  A stop requested while connecting raises no alarm. When `--ru_forward`
  runs alongside, the forwarder leaves 1003 to the session so the alarm has
  a single writer.
- **Surface**: `session_state["ru_mplane_state"]` (the phase) and
  `["ru_supervised"]` (True exactly while SUPERVISED) on the shared
  `AppState`, an INFO log line per phase transition (`RU M-plane session:
  CONNECTING -> SUPERVISED`; within one outage the retry transitions and the
  repeated connect failure after the first log at DEBUG), the alarms
  above, and a `stats` counter dict (`connect_cycles`, `supervision_notifications`, `watchdog_resets`,
  `watchdog_rpc_errors`, `starvations`, `other_notifications`,
  `callhome_accepts`).

Extension seams: `register_notification_handler(fn)` receives non-supervision
notifications (exceptions isolated) and `register_cycle_handler(fn)` runs with
the `RuConfig` client once per cycle after subscribing (transport failures
recycle the session, handler bugs are isolated). Nothing in this tree
registers on them yet; array-carrier state-change notifications are logged
at INFO as the activation receipt regardless.

### Call-home (`--ru_callhome`)

Some O-RUs accept no inbound NETCONF at all — they dial their manager
(RFC 8071; WG4 Mplane startup discovers the manager address via DHCP).
With `--ru_callhome` the session binds a listener (default `0.0.0.0:4334`)
and waits for the O-RU's dial-in instead of connecting out. Only the
transport roles invert: the O-RU remains the SSH server on the connection
it initiated, so authentication is unchanged. The O-RU controls how many
connections exist and a persistent call-home policy makes exactly one, so
the command and notification roles share the accepted session — gated on
the O-RU advertising `:interleave` (RFC 5277); a peer without it is closed
and waited out rather than half-driven. Reconnect inverts too: the
listener stays bound across cycles and recovery is the O-RU re-dialing on
its re-call-home timer. Everything above the transport — supervision,
alarms, the state surface — is unchanged. A peer that connects but never
completes the NETCONF hello holds the connect cycle for up to the RPC
timeout (30 s) before the listener waits for the next dial-in.

## Running it

```
python3 src/o1_adapter.py --profile ru --ru_supervise \
    --ru_netconf_host <ru> --ru_netconf_port 830 \
    --ru_netconf_username <user> --ru_netconf_password <pass> \
    [--ru_callhome --ru_callhome_bind 0.0.0.0 --ru_callhome_port 4334] \
    [--ru_supervision_interval 60 --ru_supervision_guard 10] [--rpc_log FILE]
```

`--profile ru` skips the DU-facing loops — the NETCONF session toward a
DU/gNB and the PM-telemetry websocket — which would otherwise only spin a
permanent connect-retry and alarm 1001/1002 flap against nothing
(`mplane_session.du_facing_loops_enabled`; `--ru_forward` keeps the NETCONF
loop as its one RU-profile consumer). With any other profile the session
runs alongside those loops.

| Flag | Default | Meaning |
|---|---|---|
| `--ru_supervise` | off | own a resident supervised Mplane session |
| `--ru_netconf_host/_port/_username/_password` | 10.10.0.192/830/—/— | O-RU NETCONF endpoint (the host keeps the adapter's existing default and is not dialed with `--ru_callhome`; credentials required with `--ru_supervise` or `--ru_forward`) |
| `--ru_callhome` | off | accept the O-RU's call-home (RFC 8071) instead of dialing out; needs the O-RU to advertise `:interleave`; requires `--ru_supervise` |
| `--ru_callhome_bind` / `--ru_callhome_port` | 0.0.0.0 / 4334 | call-home listener address and port |
| `--ru_supervision_interval` / `--ru_supervision_guard` | 60 / 10 | o-ran-supervision timers, seconds (uint16-validated) |
| `--ru_datastore` | running | O-RU datastore |
| `--rpc_log` | off | append the raw NETCONF conversation (every rpc, rpc-reply and notification) to a file |

## What it deliberately does not do

- **Provisioning**: the session sends no configuration. The O-RU is
  provisioned with the CLI (`src/ru_controller.py`) or through the
  `--ru_forward` path; the cycle-handler seam is where provision-on-connect
  would attach.
- **Fault and performance bridging**: o-ran-fm and
  o-ran-performance-management notifications reach the notification-handler
  seam but nothing consumes them yet.
- **`--ru_forward` unification**: the DU→RU config forwarder keeps its own
  session; only the connection alarm is shared (single writer, see above).
  Forwarder connect failures are then logged only — no alarm id covers the
  forwarder while the session owns 1003.
- **Software/file/user/certificate management** — see the client document's
  coverage table ([mplane-client.md](mplane-client.md)).

## Known limits

An O-RU that keeps its own supervision timers without reporting
`next-update-at` (an accepted reset whose reply carries only
`error-message`) is supervised against the local budget: when its
notification interval exceeds `--ru_supervision_interval` +
`--ru_supervision_guard`, the session trips starvation — DEGRADED, alarm
1004 — on every cycle. Set `--ru_supervision_interval` to the O-RU's value.

## Verification

The session lifecycle and call-home paths are covered by the companion test
repository: scripted fake sessions drive the state machine, watchdog
semantics and alarm bookkeeping without an O-RU, and the same session runs
against the simulated O-RU (netopeer2/sysrepo loaded with the WG4 models).
The simulator has no application behind `supervision-watchdog-reset` — every
reset is answered with an rpc-error — which deliberately exercises the
alive-but-rejected path: the session must hold the connection without
claiming SUPERVISED. Accepted resets need an O-RU that implements the RPC;
supervision-notification round trips are covered by injecting notifications
into the simulator where docker can reach it. See
[mplane-sim-testing.md](mplane-sim-testing.md).
