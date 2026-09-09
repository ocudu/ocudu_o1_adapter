<!--
SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
SPDX-FileCopyrightText: Copyright (C) 2026 OCUDU contributors
SPDX-License-Identifier: BSD-3-Clause-Open-MPI
-->

# Mplane sim loop — testing path

How to exercise the Mplane client and the adapter's resident session
end-to-end **without a real O-RU**, using a simulated O-RU NETCONF server.
This is the basis for the unit and integration tests in the companion test
repository.

## The two components

| Role | What | Where it comes from |
|---|---|---|
| **Mplane client** | the `RuConfig` class in `src/ru_config.py`, the stand-alone CLI in `src/ru_controller.py` and the adapter's resident session in `src/mplane_session.py` (`--ru_supervise`) — a NETCONF/YANG client that provisions an O-RU, reads it back and supervises it | **this repo** (the code under test) |
| **Simulated O-RU (server)** | `ocudu_netconf` running `--config ru` — netopeer2/sysrepo loaded with the O-RAN WG4 fronthaul YANG (its `setup_ru.sh`); acts as an O-RU's Mplane server on `:830` | the **`ocudu_netconf` repo** (separate), consumed as a prebuilt image — you do *not* fork it, you run it as a fixture |

Build the image from the `ocudu_netconf` repo as its README describes
(`docker build -t ocudu-netconf/ocudu-netconf:latest .`) or pull the
`netconf_amd64` image its CI publishes under the project's registry. The
sim loads the standard O-RAN WG4 YANG (`o-ran-uplane-conf`,
`-processing-element`, `-delay-management`, `-supervision`, `-module-cap`,
`-fm`, `-sync`, …) with the features `CONFIGURABLE-TDD-PATTERN-SUPPORTED`,
`SUPERVISION-WITH-SESSION-ID` and `GNSS` enabled. NETCONF/SSH listens on
`:830`; the default account is documented in the `ocudu_netconf` repo.

## Stand up the loop

```bash
# 1. client deps (once)
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. sim O-RU server
docker rm -f ocudu-sim-ru 2>/dev/null
docker run -d --name ocudu-sim-ru -p 830:830 ocudu-netconf/ocudu-netconf:latest --config ru
# ready when the logs show the SSH listener on 0.0.0.0:830
docker logs -f ocudu-sim-ru | grep -m1 "Listening on"

# 3. client against the sim
.venv/bin/python src/ru_controller.py --host localhost --port 830 -u <user> -p <pass> --get_config
.venv/bin/python src/ru_controller.py --host localhost --port 830 -u <user> -p <pass> --set_full_config \
  --ru_mac_addr <ru-mac> --vlan 127 --du_mac_addr <du-mac> \
  --iq_bitwidth 9 --compression_type STATIC --rf_bandwidth_hz 100000000 \
  --dl_arfcn 649980 --dl_freq 3749700000 --ul_arfcn 649980 --ul_freq 3749700000 \
  --tx_gain 39 --carrier_state ACTIVE
# read back -> regenerated DU ru_ofh config (timing windows from o-ran-delay-management):
.venv/bin/python src/ru_controller.py --host localhost --port 830 -u <user> -p <pass> --get_config

# 4. the adapter's resident session against the sim
.venv/bin/python src/o1_adapter.py --profile ru --ru_supervise \
  --ru_netconf_host localhost --ru_netconf_port 830 \
  --ru_netconf_username <user> --ru_netconf_password <pass> --rpc_log /tmp/ru-rpc.log
# watch the log: "RU M-plane session: DISCONNECTED -> CONNECTING" and the alarm lines

# cleanup
docker rm -f ocudu-sim-ru
```

`--dry-run --log-level DEBUG` renders every payload with **no server** —
useful for template tests. `--rpc_log FILE` (CLI and adapter) captures the
raw NETCONF conversation for inspection.

What to expect from the session against the sim: the sim has no application
behind `supervision-watchdog-reset`, so every reset is answered with an
rpc-error ("no matching subscribers"). The session holds both connections
but never reports SUPERVISED (a rejected reset did not arm the O-RU's
watchdog), emits no supervision-notifications on its own, and therefore
degrades to `DEGRADED` with alarm 1004 after interval + guard seconds; alarm
1003 clears as soon as the pair is connected and subscribed, rejected resets
notwithstanding. That is the alive-but-rejected path, exercised
deliberately; accepted resets need an O-RU that implements the RPC, and
supervision-notification round trips are injected into the simulator where
docker can reach it.

### Call-home loop

A simulator build with call-home support (the `ocudu_netconf` README's
`--enable-callhome <host>[:<port>]`; not every published image carries it)
emulates a call-home-only O-RU (RFC 8071): it dials the given manager
persistently, re-dialing when the connection drops. With docker's default
bridge, the host is reachable from the container at the bridge gateway
(typically `172.17.0.1`):

```bash
docker run -d --name ocudu-sim-ru-ch ocudu-netconf/ocudu-netconf:latest \
  --config ru --enable-callhome 172.17.0.1:4334
# adapter side: --ru_supervise --ru_callhome (listener on 0.0.0.0:4334),
# no --ru_netconf_host needed — the sim dials in.
```

The companion test repository's call-home integration test is gated on
`MOCK_RU_CALLHOME_PORT` (set it to the port the sim is dialing).

## What the sim validates (and what it does NOT)

| Layer | Verified against the sim? |
|---|---|
| NETCONF session / `<hello>` / capabilities | yes |
| `edit-config` payloads schema-valid against real WG4 YANG (sysrepo rejects bad leaves) | yes |
| `get-config` round-trip + `ofh_config_builder` output | yes |
| RPC / `create-subscription` structure (supervision) | accepted, but **no behavioral backend** — resets are rejected, no notifications are emitted |
| session lifecycle: reconnect, alive-but-rejected handling, idle keepalive, call-home | yes (rpc-error path only) |
| RU behaviour: RF on-air, supervision timeout, real FM alarms, real delay values, vendor quirks | **NO — needs a real O-RU** |

So: the sim is a **schema/protocol/CM fixture**, not a behavioural radio.
Good for correctness of the client's exchange; not for RU behaviour.

## Writing automated tests

`RuConfig` is importable (`from ru_config import RuConfig`; `ru_controller`
re-exports it for the CLI) and so is `MplaneSession`
(`from mplane_session import MplaneSession`), so tests import them directly
rather than shelling the CLI. `MplaneSession` exposes two test seams:
`connect_factory` replaces the real NETCONF connect with a scripted session
double, and `poll_cap` bounds each notification wait so a stop request is
honoured promptly.

Pattern (pytest):
```python
# conftest.py — session fixture boots the sim container, tears it down
@pytest.fixture(scope="session")
def sim_ru():
    # docker run ... --config ru ; wait for :830 ; yield host/port/creds ; docker rm -f
    ...

# test — drive the client, assert on the resulting datastore / generated config
def test_full_config_roundtrip(sim_ru):
    mgr = ncclient.manager.connect(host=sim_ru.host, port=830, username=sim_ru.user, password=sim_ru.password,
                                   hostkey_verify=False, look_for_keys=False, allow_agent=False)
    ru = RuConfig(mgr, "running")
    ru.set_full_config(profile_dict)          # edit-config; sysrepo rejects invalid -> test fails
    ofh, cell = build_ofh_config(*ru_reads)   # read back
    assert ofh["cells"][0]["ru_mac_addr"] == profile_dict["interface"]["ru_mac_addr"]
```
Test ideas: each `set_*` accepted; round-trip fidelity; `ofh_config_builder`
field mapping; capability gating (`_is_configurable_tdd_supported`);
supervision RPC encoding (`reset_supervision_watchdog` against the sim raises
the rpc-error, a malformed variant is rejected by schema validation);
`MplaneSession` state transitions and alarm bookkeeping with scripted
sessions; **regression: generated config matches a golden generated config**.
Fault and performance handling on the session are not exercised yet — the
handler seams exist, nothing consumes them.
