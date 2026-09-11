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
(and northbound to VES through the existing alarm notifier). With
`--ru_provision_config` it also provisions the O-RU on every (re)connect,
activating carriers only once the O-RU reports itself synchronized, and it
mirrors the O-RU's faults into its alarm registry. The O-RU becomes a managed
element rather than a one-shot edit-config target; the same client library
remains available as a CLI ([mplane-client.md](mplane-client.md)) and through
the `--ru_forward` path.

```
                        ┌─────────────────── o1_adapter ───────────────────┐
   O-RU  ◄──NETCONF──►  │  MplaneSession ──► AlarmManager ──► VES          │
  (server)              │  (2 sessions,   ──► AppState.session_state       │
                        │   watchdog,          + INFO log per transition   │
                        │   state machine)──► RuProvisioner ─► RuConfig    │
                        │                 ──► RuFaultBridge ─► AlarmManager│
                        └──────────────────────────────────────────────────┘
```

## The session (`mplane_session.py`)

Two NETCONF sessions per O-RU — a **command session** (RPCs, edits, reads)
and a **notification session** (`create-subscription` puts a session into
notification mode, so the roles cannot share one without `:interleave`).
Supervision is the exception to that split: o-ran-supervision watchdog
timers are per NETCONF session, held by the session that subscribed, so
`supervision-watchdog-reset` is dispatched on the notification session
(`RuConfig(..., supervision_manager=)`). That client acts as the account role
given by `--ru_role`: `sudo` (default) or `hybrid-odu` per the O-RAN WG4 M-plane
specification, Table 6.5-1 — see the client document's Roles section for what
each role writes. Lifecycle:

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
the `RuConfig` client once per connect cycle, after the initial watchdog reset
is answered — accepted or not — and before the notification loop starts
(transport failures recycle the session, handler bugs are isolated). While the
handlers run the session keeps the O-RU's watchdog fed on its own schedule — a
`supervision-watchdog-reset` every half interval, on the notification session,
so nothing interleaves with the handlers' RPCs on the command session — which
is what lets a handler wait minutes for PTP lock or for an interface the
management plane has yet to create; once they return the session resets the
watchdog once more and derives its starvation budget from that reply, since
every feed restarted the O-RU's timer from an instant it only scheduled.
`--ru_provision_config` registers the provisioner on the cycle seam and the
fault bridge (below) sits on both seams; array-carrier state-change
notifications are logged at INFO as the activation receipt regardless.

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

## Provision-on-connect (`ru_provisioner.py`)

`--ru_provision_config <yaml>` names a full-config file applied as the first
cycle handler on **every** connect cycle. That is the recovery model: an O-RU
reboot drops the session and returns with an empty datastore, so
reconnect-and-reprovision heals it, and NETCONF merges make re-provisioning an
already-configured O-RU a no-op. The file is validated when the adapter starts
— required sections and leaves, the provisioning switches as real booleans (a
quoted `"false"` is a startup error, not a truthy string), port-id lists, the
endpoint and switching-point declarations and the TDD pattern — so a malformed
profile is a usage error, not something rediscovered on every cycle. The
sections are the `set_full_config` dict described in the client document's
"Configuration dict" section, which holds the per-key semantics:

```yaml
interface:  {ru_mac_addr: "...", vlan: 5, base_interface: eth0, l2_mtu: 9600}  # base_interface, l2_mtu optional
processing: {ru_mac_addr: "...", du_mac_addr: "...", vlan: 5}     # same vlan and ru_mac_addr as interface; optional interface_name (hybrid-odu, see below)
endpoint:
  iq_bitwidth: 9
  compression_type: STATIC
  num_prb: 273
  frame_structure: 193
  dl_port_id: [0, 1, 2, 3]   # optional eAxC layout, defaults to legacy 4x4
  ul_port_id: [0, 1]
  prach_port_id: [4, 5]
  # optional endpoint naming, shown with example values (the defaults are the
  # legacy generated names) — O-RUs with fixed static endpoints only accept
  # their exact names
  tx_endpoint_prefix: LowLevelTxEndpoint
  rx_endpoint_prefix: LowLevelRxEndpoint
  prach_endpoint_prefix: LowLevelRxPrachEndpoint
  endpoint_index_base: 0
  # ... or declare the names outright when prefix+index cannot express them
  # (zero-padded, non-consecutive, type-blind names). Explicit lists take
  # precedence per group and also classify the endpoints on readback:
  # tx_endpoints:    [{name: PORT-00, eaxc_id: 0}, {name: PORT-10, eaxc_id: 1}]
  # rx_endpoints:    [{name: PORT-05, eaxc_id: 0}, {name: PORT-15, eaxc_id: 1}]
  # prach_endpoints: [{name: PORT-25, eaxc_id: 4}, {name: PORT-35, eaxc_id: 5}]
carrier:
  dl_arfcn: 637212
  dl_freq: 3558180000
  ul_arfcn: 637212
  ul_freq: 3558180000
  tx_gain: 39
  rf_bandwidth_hz: 100000000
tdd: {pattern_upload: true, carrier_binding: true}   # optional; both default to true.
                              # The pushed pattern defaults to the canonical
                              # 7d1s2u (6/4/4) at 30 kHz; OCUDU tdd_ul_dl_cfg
                              # fields (scs_khz, dl_ul_tx_period, nof_dl_slots,
                              # nof_dl_symbols, nof_ul_slots, nof_ul_symbols)
                              # recompute it as a complete spec, and a raw
                              # switching_points list
                              # ([{direction, frame_offset[, switching_point_id]}])
                              # bypasses computation for O-RUs that validate
                              # boundaries differently. Set carrier_binding: false
                              # for firmware that never answers the binding edit:
                              # the reply timeout is a transport failure, so the
                              # session would recycle and re-provision endlessly.
activation: {state: ACTIVE, tolerate_reply_timeout: true}   # optional; these are the defaults
```

`interface` is required even when the session acts as `hybrid-odu`:
`set_full_config` reads it unconditionally, and the loader checks that
`interface` and `processing` agree on the VLAN and the O-RU MAC address because
the processing element names the VLAN interface (`uc-vlan<vlan>`) the interface
step creates. Under `hybrid-odu` the interface write itself is skipped with one
INFO line: the interface is the management plane's, and the processing
element's `interface-name` is a leafref into it, so the provisioner binds the
element to `processing.interface_name` when the profile declares it (the name
the management plane uses) and otherwise to the l2vlan interface carrying
`processing.vlan`, read from the O-RU. It waits up to `--ru_interface_timeout`
(default 300 s; 0 reads once) for that interface to exist; a cycle that never sees it
pushes nothing, logs a warning and leaves the rest to the next connect cycle,
instead of pushing an element the O-RU must reject. `activation.tolerate_reply_timeout`
defaults to true on this path — unlike `set_full_config` and the CLI, where it
defaults to false — because the activation is re-applied on every connect cycle
and some O-RU NETCONF servers accept the edit but never reply once the carriers
are already active; the array-carriers readback, not the edit reply, is the
receipt.

Carrier activation is gated on synchronization (a WG4 activation
precondition; sync state is read-only on many O-RUs): the provisioner polls
for `sync-state LOCKED` up to `--ru_sync_timeout` (default 300 s; 0 reads once
and never waits) and leaves the carriers inactive with a warning if it never
arrives. A profile with `activation.state: INACTIVE` skips the wait and writes
the carriers inactive right away. Two properties of the wait matter
operationally:

- the provisioner **never feeds the supervision watchdog itself**: a cold-boot
  PTP lock lawfully outlasts the supervision budget, and the session feeds the
  watchdog while its cycle handlers run (above), so the wait is just a wait;
- the sync read is **strict**, so a session that dies mid-wait raises and
  recycles immediately instead of being polled as not-yet-LOCKED.

After activation the array-carriers state is polled until every carrier
reports READY (bounded) and the resulting
`{carrier: state}` map is logged as the receipt — INFO when complete, WARNING
otherwise. An rpc-error from any provisioning edit or strict read is logged
and does **not** recycle the session: a reconnect would re-push the identical
configuration and fail again forever, starving supervision; the next connect
cycle re-provisions. Transport failures recycle the session like any other
cycle handler. An O-RU that refuses activation while it considers the session
unsupervised (one that rejected the initial watchdog reset, for instance) is
therefore provisioned but inactive until the next connect cycle.

## Fault bridge (`ru_fm_bridge.py`)

Mirrors o-ran-fm faults into the alarm registry: adapter alarm id =
**4000 + fault-id** (o-ran-fm's fault-id is a uint16, so 4000-69535 is
reserved; definitions are registered dynamically — the fault space is
O-RU-defined), the o-ran-fm severity literal mapped by name onto the adapter's
severities (anything else becomes INDETERMINATE), the type from the O-RU's
`alarm-type` (o-ran-fm 2024-08-12; EQUIPMENT when it reports none). It is
registered under `--ru_supervise` with no flag of its own: on the notification
seam for live `alarm-notif`s, and on the cycle seam after the provisioner for
reconciliation.

- Identity per o-ran-fm is **(fault-id, fault-source)**. One adapter alarm
  mirrors a fault-id: it carries the most severe of its sources' severities,
  names the sources in its message, stays active while any source reports
  the fault and retires when the last one clears. A clear for a fault the
  bridge never mirrored cannot be attributed and is left to reconciliation.
- **Reconciliation** on every connect cycle syncs against the O-RU's
  active-alarm-list, so faults raised or cleared while the session was down
  are neither lost nor leaked. Each listed fault-id is applied once,
  aggregated over its sources, so an unchanged list emits nothing northbound.
  Staleness is judged against the alarm manager's active state — the record
  of what went northbound — so a recreated bridge starts consistent. The read
  is strict: a transport failure or reply timeout recycles the session and an
  rpc-error (the O-RU refusing the read) keeps the mirror as it is until the
  next cycle; neither is taken for "no alarms", which would clear every
  mirrored fault. Northbound delivery is synchronous; a notifier failure on
  one update does not stop the rest of the sweep, and the session keeps the
  watchdog fed meanwhile.
- The bridge's bookkeeping mutates before each manager call, so a notifier
  that raises cannot leave bridge and registry disagreeing; the event itself
  is the notifier's to deliver (the VES notifier logs and swallows delivery
  failures).
- Per o-ran-fm, WARNING faults never receive a clear notification; a WARNING
  is retired only by reconciliation, i.e. at the next connect cycle — on a
  healthy session that can be a long time.
- Nothing acts on a mirrored fault; a carrier the O-RU deactivates is
  re-activated only on the next connect cycle (see below).

**Northbound mapping.** The mirrored fault leaves through the adapter's
notifier, `ves.send_alarm_event`, as a 3GPP TS 28.532 `notifyNewAlarm` /
`notifyClearedAlarm` carried in a VES stndDefined event under the DU's
identity (`sourceName`, `eventId`, `nodeId`): the O-RU is a component of this
managed element, and a managed element per O-RU would be a topology decision
above the adapter. What the O-RU reported travels in the notification's own
fields (the bridge attaches the o-ran-fm leaves to each registry update as
`details`; `alarm_manager` carries them on the event):

| o-ran-fm | VES `stndDefinedFields.data` |
|---|---|
| fault-id | `alarmId` = 4000 + fault-id; `additionalInformation.fault-id` |
| fault-source (every source reporting the fault-id) | `additionalInformation.fault-source` |
| affected-objects | `additionalInformation.affected-objects` |
| fault-severity | `perceivedSeverity` (`CLEARED` on a clear) |
| is-cleared | `notificationType`: `notifyNewAlarm` / `notifyClearedAlarm` |
| fault-text | `specificProblem`; `additionalText` carries it with the sources |
| event-time | `eventTime` (the O-RU's timestamp; the adapter's send time when absent) |
| alarm-type | `alarmType` (the O-RU's, one of the ten X.733 types; `EQUIPMENT_ALARM` when it reports none) |
| probable-cause | `probableCause` (the O-RU's; `RU_FAULT_<fault-id>` when it reports none) |
| specific-problem | `specificProblem` (the O-RU's; its fault-text when it reports none) |
| proposed-repair-actions | `proposedRepairActions` (the O-RU's; omitted when it reports none) |
| additional-information | `additionalInformation`: each identifier/information pair, next to the leaf-name keys above |
| — | `trendIndication` from the registry; `notificationId` = the notifier's per-event sequence |

The DU's own alarms go through the same path and gain the TS 28.532
`alarmType` literals, the clear notification type, the per-event
`notificationId` and the ISO 8601 `eventTime`; no canned
`proposedRepairActions` is rendered for them any more.

Against the simulated O-RU the bridge reads an empty active-alarm-list (the
simulator raises no faults of its own); live `alarm-notif`s can be injected
into it with `sysrepocfg --notification` where docker reaches the container
([mplane-sim-testing.md](mplane-sim-testing.md)).

## Running it

```
python3 src/o1_adapter.py --profile ru --ru_supervise \
    --ru_netconf_host <ru> --ru_netconf_port 830 \
    --ru_netconf_username <user> --ru_netconf_password <pass> \
    [--ru_provision_config ru.yaml --ru_sync_timeout 300 --ru_interface_timeout 300] [--ru_role hybrid-odu] \
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
| `--ru_provision_config` | none | full-config YAML applied on every connect cycle (requires `--ru_supervise`, refused with `--ru_forward`; validated at startup) |
| `--ru_sync_timeout` | 300 | seconds to wait for sync-state LOCKED before leaving the carriers inactive (0 = read once) |
| `--ru_interface_timeout` | 300 | seconds to wait, as `hybrid-odu`, for the management plane's VLAN interface before deferring provisioning to the next connect cycle (0 = read once) |
| `--ru_role` | sudo | account role the session's client acts as (`sudo` or `hybrid-odu`, Table 6.5-1); requires `--ru_supervise`; `hybrid-odu` is refused with `--ru_forward`, whose writes are `sudo` |
| `--ru_datastore` | running | O-RU datastore |
| `--rpc_log` | off | append the raw NETCONF conversation (every rpc, rpc-reply and notification) to a file |

## What it deliberately does not do

- **Mid-session re-activation**: a carrier the O-RU deactivates while the
  session is up (an availability-state FAULTY episode) is re-activated only on
  the next connect cycle — an O-RU leaving FAULTY after a critical fault
  typically resets, which lands in the reconnect-and-reprovision path — not by
  watching alarm clears.
- **Performance bridging**: o-ran-performance-management notifications reach
  the notification-handler seam but nothing consumes them yet.
- **Cell-driven carrier state**: the DU cell's administrative state does not
  yet drive the O-RU carriers through this session; that is later work that
  needs the session under a DU-facing profile.
- **`--ru_forward` unification**: the DU→RU config forwarder keeps its own
  session and writes as `sudo`; it and `--ru_provision_config` are alternative
  ways to source the O-RU's configuration and are refused together. Only the
  connection alarm is shared (single writer, see above); forwarder connect
  failures are then logged only — no alarm id covers the forwarder while the
  session owns 1003.
- **Software/file/user/certificate management** — see the client document's
  coverage table ([mplane-client.md](mplane-client.md)).

## Known limits

An O-RU that keeps its own supervision timers without reporting
`next-update-at` (an accepted reset whose reply carries only
`error-message`) is supervised against the local budget: when its
notification interval exceeds `--ru_supervision_interval` +
`--ru_supervision_guard`, the session trips starvation — DEGRADED, alarm
1004 — on every cycle. Set `--ru_supervision_interval` to the O-RU's value.

The session's feeds during the cycle handlers are sent on the notification
session every half interval regardless of what the handler is doing, so a slow
edit on the command session cannot starve the watchdog; on a call-home pair
both roles share one session and the O-RU serves the RPCs in order, so a feed
queued behind a long edit arrives when that edit completes — still inside the
budget for any edit bounded by the RPC timeout. Those feeds count in
`stats["watchdog_resets"]` like every other reset.

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
into the simulator where docker can reach it. Provision-on-connect is covered
the same two ways: scripted fakes for the sync gating and rpc-error handling, and the simulator for the applied configuration — it never
reports sync LOCKED, so the suite proves provisioned-but-inactive, and a leaf
perturbed between cycles is restored after a forced recycle, the proof that a
reconnect genuinely re-provisions. The LOCKED and READY paths need an O-RU.
The fault bridge is covered by unit tests over a real alarm manager
(raise/clear, reconcile including a refused and a failed read, multi-source
faults, WARNING retirement, a failing notifier), a scripted-session wiring test, the empty-list read against the
simulator, and injected `alarm-notif` round trips where docker reaches the
simulator.
See [mplane-sim-testing.md](mplane-sim-testing.md).
