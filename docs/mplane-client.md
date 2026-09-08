<!--
SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
SPDX-FileCopyrightText: Copyright (C) 2026 OCUDU contributors
SPDX-License-Identifier: BSD-3-Clause-Open-MPI
-->

# O-RU Mplane client

The adapter's Mplane client for O-RAN WG4 O-RUs is split in two:
`src/ru_config.py` holds the `RuConfig` library class (also used by the
adapter's `--ru_forward` path), and `src/ru_controller.py` is the stand-alone
CLI for interactive bring-up and debugging. Everything it writes is validated
by the O-RU's own YANG schemas; everything it derives is computed from the
published O-RAN WG4 models and 3GPP tables.

## What it covers

| Area | Model | Support |
|---|---|---|
| Fronthaul provisioning | o-ran-uplane-conf, o-ran-processing-element, ietf/o-ran-interfaces | full write path: interfaces, processing element, endpoints, carriers, links, TDD, activation |
| Delay management | o-ran-delay-management | read + DU-side timing derivation |
| Capability discovery | o-ran-module-cap, ietf-yang-library | read (RFC 8525 with RFC 7895 modules-state fallback) |
| Supervision | o-ran-supervision | watchdog reset RPC + notification-driven supervise loop |
| Performance management | o-ran-performance-management | measurement-object configuration (two-phase) |
| Synchronization | o-ran-sync | status read, sync-LOCKED wait; ptp-config write where the O-RU permits it |
| Not implemented | fault management, software/file management, user management, certificates, trace/troubleshooting, ALD, beamforming | out of scope — use vendor tooling or the O-RU's own procedures |

## Provisioning

`set_full_config()` applies a complete fronthaul configuration in the order
a validating O-RU requires (later objects hold YANG references to earlier
ones):

1. VLAN interface (`ietf-interfaces` + `o-ran-interfaces`)
2. processing element (its transport-flow must match the interface)
3. low-level TX/RX endpoints (compression, frame parameters, e-axcid)
4. TX/RX array carriers (`type NR`, `n-ta-offset`, RF parameters)
5. low-level links (which commit MAC/VLAN/CoS towards hardware)
6. TDD pattern upload **and per-carrier binding** — a configurable TDD
   pattern applies only to carriers referencing it via the
   `configurable-tdd-pattern` leafref, and pattern validation happens at
   carrier activation; both are skipped when the O-RU does not advertise
   `CONFIGURABLE-TDD-PATTERN-SUPPORTED`
7. carrier activation — all carriers in a single edit-config;
   `skip_activation=True` defers this step so callers can gate it on
   synchronization (`activate_full_config()` applies it later)

The eAxC layout is data, not code: `dl_port_id` / `ul_port_id` /
`prach_port_id` lists select the eAxC assignment and drive endpoint, carrier,
link and activation entry counts (defaults preserve the legacy 4x4 layout;
PRACH keeps its deliberate crossed carrier pairing). Endpoint **names** are
data too — they are the O-RU's fixed device data, and not every vendor's
names fit prefix+index generation (zero-padded, non-consecutive,
type-blind) — so explicit `tx_endpoints` / `rx_endpoints` /
`prach_endpoints` lists (`[{name, eaxc_id}]`) take precedence over the
generated prefix+index names, and the same declarations drive rx-endpoint
classification on readback (which endpoint carries PRACH is an operator
assignment, not something names reliably reveal — `--prach_endpoint_names`
supplies it to `--get_config`). `n-ta-offset` defaults to 25600 Tc, the
3GPP TS 38.133 Table 7.1.2-2 value for FR1 FDD and for FR1 TDD without
LTE-NR coexistence; `--n_ta_offset` (config `carrier.n_ta_offset`) sets 0
for FR1 FDD with LTE-NR coexistence, 39936 for FR1 TDD with LTE-NR
coexistence or 13792 for FR2.

## Configuration dict

`set_full_config(config_dict)` takes one dict with six sections; the CLI
builds it from its options, library callers pass it directly:

- `interface`: `ru_mac_addr`, `vlan`, optional `base_interface` (the
  physical port the VLAN interface rides on; the template defaults it).
- `processing`: `ru_mac_addr`, `du_mac_addr`, `vlan`.
- `endpoint`: `iq_bitwidth`, `compression_type`, `num_prb`,
  `frame_structure`, optional `prach_frame_structure` / `prach_num_prb`;
  the eAxC layout either as `dl_port_id` / `ul_port_id` / `prach_port_id`
  lists with the generated-name knobs `tx_endpoint_prefix` /
  `rx_endpoint_prefix` / `prach_endpoint_prefix` and `endpoint_index_base`,
  or as explicit `tx_endpoints` / `rx_endpoints` / `prach_endpoints`
  declarations (`[{name, eaxc_id}]`), which take precedence.
- `carrier`: `dl_arfcn`, `dl_freq`, `ul_arfcn`, `ul_freq`, `tx_gain`,
  `rf_bandwidth_hz`, optional `n_ta_offset` (Tc; default 25600) and
  `nof_carriers`.
- `tdd`: the pattern in OCUDU `tdd_ul_dl_cfg` terms (`scs_khz`,
  `dl_ul_tx_period`, `nof_dl_slots`, `nof_dl_symbols`, `nof_ul_slots`,
  `nof_ul_symbols`) or a raw `switching_points` list, optional
  `tdd_pattern_id` (default 1), and the toggles `pattern_upload` and
  `carrier_binding` (both default true; set them false for firmware that
  manages TDD from its own device configuration).
- `activation`: `state` (`ACTIVE` / `INACTIVE`) and
  `tolerate_reply_timeout` (default false; true turns an unanswered
  activation edit from an error into a warning).

## Derivations

The client computes DU-side configuration from what the O-RU advertises —
the reason Mplane exists:

- **Delay management** (`ofh_config_builder.build_ofh_timing`): the O-RU's
  `ru-delay-profile` (nanoseconds, per bandwidth/SCS — the entry matching
  the active carrier numerology is selected) plus fronthaul transport bounds
  yield the OCUDU `ru_ofh` timing windows in microseconds:

  ```
  T1a_min_x = T2a_min_x + T12_max      (DL transmit windows; x = up / cp-dl / cp-ul)
  T1a_max_x = T2a_max_x + T12_min
  Ta4_min   = Ta3_min   + T34_min      (UL reception window)
  Ta4_max   = Ta3_max   + T34_max
  ```

  Rounding is conservative: DL windows only tighten (min up, max down), the
  UL window only widens. `--get_config` prints the resulting gnb-yaml
  snippet; `--t12-min/--t12-max/--t34-min/--t34-max` supply the transport
  bounds in microseconds. The read is skipped, with an INFO line, when the
  O-RU does not list `o-ran-delay-management` in its yang-library.
- **TDD switching points** (`compute_tdd_switching_points`): exact integer
  arithmetic in o-ran-uplane-conf frame-offset units (1/1.2288 GHz ticks,
  10 ms frame = 12288000) from the OCUDU `tdd_ul_dl_cfg` field shape,
  covering the 3GPP TDD-UL-DL-ConfigCommon pattern space. Offsets land on
  **CP-inclusive TS 38.211 symbol boundaries** — symbols are not uniform
  (the first symbol of each half-subframe carries the extended CP), so a
  uniform slot/14 grid puts every intra-slot point mid-symbol and a
  boundary-validating O-RU rejects it. O-RUs that validate differently can
  bypass computation with a raw `switching_points` list
  (`[{direction, frame_offset[, switching_point_id]}]`; the
  `switching-point-id` follows list order unless every entry names its own:
  all-or-none, unique, uint16).
- **Frame parameters**: `compute_num_prb` (3GPP TS 38.104 table 5.3.2-1)
  and `compute_frame_structure` (FFT-size nibble | SCS nibble) replace
  hand-maintained tables.

## Supervision and sync

- `supervise(interval, guard)`: notification-driven watchdog loop — resets
  on each supervision-notification, never on a blind timer.
- `get_sync_status(strict=)` / `wait_for_sync_locked(timeout)`: carrier
  activation requires a synchronized O-RU (WG4 activation precondition) and
  synchronization state is read-only on many O-RUs — the client can only
  wait, not configure. `--wait-sync-locked SECONDS` gates
  `--activate_carriers` accordingly. `get_array_carriers_state()` /
  `wait_for_carriers_ready(timeout)` read back the asynchronous carrier
  state (DISABLED -> BUSY -> READY): the readback, not the edit reply, is
  the activation receipt (`--wait-carriers-ready SECONDS`).
- Reads request `with-defaults=report-all` (RFC 6243) when the server's
  with-defaults capability lists that mode (as `basic-mode` or under
  `also-supported`): with basic-mode `explicit`, default-valued leaves such
  as a fresh carrier's `active` state are absent from readbacks unless
  report-all is requested, and ncclient refuses to send a mode the server
  did not list.

## CLI quick reference

```
# read everything + print the derived DU ru_ofh snippet
./ru_controller.py --host <ru> -u <user> -p <pass> --get_config --t12-max 100 --t34-max 100

# full provisioning (defaults: legacy 4x4 eAxC layout)
./ru_controller.py --host <ru> -u <user> -p <pass> --set_full_config \
    --ru_mac_addr ... --du_mac_addr ... --vlan ... \
    --iq_bitwidth 9 --compression_type STATIC --rf_bandwidth_hz 100000000 \
    --dl_arfcn ... --dl_freq ... --ul_arfcn ... --ul_freq ... --tx_gain ...

# activation gated on PTP lock, then wait for the carriers to report READY
./ru_controller.py --host <ru> ... --activate_carriers --carrier_state ACTIVE \
    --wait-sync-locked 300 --wait-carriers-ready 60

# keep the supervision session alive / configure PM
./ru_controller.py --host <ru> ... --supervise
./ru_controller.py --host <ru> ... --set_pm
```

`--callhome` accepts an O-RU-initiated NETCONF call-home connection
(`--callhome-port`, default 4334) instead of dialing out. `--dry-run` prints
the rendered payloads without touching the O-RU.

## Known limits

- The CLI drives the default eAxC layout; alternative port layouts and
  explicit endpoint names are library-level parameters of
  `set_full_config()`.
- Writable o-ran-sync configuration is attempted only on demand
  (`set_oran_sync_config()`) and is vendor-dependent; treat sync as
  read-only unless the O-RU documents otherwise.
- One-shot by design: nothing restarts a `--supervise` loop that lost its
  session.
