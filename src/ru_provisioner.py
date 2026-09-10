# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-FileCopyrightText: Copyright (C) 2026 OCUDU contributors
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Provision-on-connect: apply a full O-RU configuration on every session cycle.

Runs as a cycle handler of the persistent M-plane session: once per connect
cycle, after the initial supervision-watchdog reset is answered — including
every reconnect after an O-RU reboot, whose running datastore starts empty
again — the configured full-config dict (interface, processing element,
endpoints, carriers, links, TDD) is re-applied. Edits are merges, so
re-provisioning an already-configured O-RU is idempotent.

Carrier activation is gated on synchronization: WG4 makes a synchronized
O-RU a precondition for carrier activation, and synchronization state is
read-only on many O-RUs — the provisioner polls for sync-state LOCKED up to
a configurable timeout and leaves the carriers inactive (with a warning)
when it never arrives, rather than provoking a guaranteed rejection. A mock
RU exposes no synchronization state at all and therefore stays unactivated,
which is the correct outcome for a radio that cannot transmit. A profile
whose activation state is INACTIVE does not wait for sync at all.

Activation ends with a receipt, not an assumption: carrier state is
asynchronous (DISABLED -> BUSY -> READY per o-ran-uplane-conf), so after the
activation edit the provisioner polls the array-carriers state until every
carrier reports READY (bounded) and logs the resulting {carrier: state} map.
An accepted edit whose carriers never reach READY is a loud warning.

The sync wait can lawfully outlast the o-ran-supervision budget (PTP lock on
a cold boot takes minutes; the budget is notification-interval + guard), and
it runs before the session's notification-driven watchdog loop starts — so
the provisioner feeds the watchdog itself on one schedule for the whole
cycle: a supervision-watchdog-reset at half the notification interval,
checked at every poll of both waits and once more before the activation
edit. rpc-error replies are alive-but-rejected (the mock RU has no
application behind the RPC); any transport failure, on the reset or on the
strict sync read, propagates so the session recycles promptly instead of
polling a dead session for the rest of the timeout.

Under the hybrid-odu role the VLAN interface is the management plane's
(ietf-interfaces is read-only for the O-DU, Table 6.5-1) and the processing
element's transport-flow/interface-name is a leafref into it: the provisioner
binds the element to processing.interface_name when the profile declares it,
else to the l2vlan interface carrying processing.vlan, and waits up to
interface_timeout_s (feeding the watchdog) for that interface to exist before
pushing anything. A cycle that never sees it pushes nothing; the next connect
cycle tries again.

Not covered here (deliberately): re-activation after an availability-state
FAULTY episode clears mid-session — carriers deactivated by the O-RU are
re-activated on the next session cycle (an O-RU leaving FAULTY after a
critical fault typically resets, dropping the session), not by watching
alarm clears.
"""

import logging
import time

from ncclient.operations import rpc as rpc_ops

from ofh_config_builder import (
    compute_tdd_switching_points,
    endpoint_naming,
    normalize_endpoint_entries,
    validate_switching_points,
)
from xml_utils import ensure_list


class RuProvisioner:  # pylint: disable=too-few-public-methods,too-many-instance-attributes
    """Apply a full-config dict to the O-RU on each connect cycle."""

    def __init__(  # pylint: disable=too-many-arguments
        self,
        provision_config,
        sync_timeout_s=300,
        sync_poll_s=5,
        *,
        supervision_interval=60,
        supervision_guard=10,
        carrier_ready_timeout_s=120,
        interface_timeout_s=300,
    ):
        self.provision_config = provision_config
        self.sync_timeout_s = sync_timeout_s
        self.sync_poll_s = sync_poll_s
        self.supervision_interval = supervision_interval
        self.supervision_guard = supervision_guard
        self.carrier_ready_timeout_s = carrier_ready_timeout_s
        self.interface_timeout_s = interface_timeout_s
        # one feed schedule per provision() call (one session, one worker
        # thread at a time), and the rejected-reset warning once per call
        self._next_feed = 0.0
        self._feed_rejected_logged = False

    def provision(self, ru_config):
        """Provision the O-RU, activating carriers only once sync is LOCKED.

        Suitable for MplaneSession.register_cycle_handler (registered first,
        so the base configuration exists before anything else on the cycle
        seam touches the O-RU). NETCONF transport failures propagate and
        recycle the session; an rpc-error reply is alive-but-rejected (an
        unsupported optional leaf, a NACM deny, a name mismatch, a strict read
        the O-RU refuses) and does NOT recycle — the reconnect would re-push
        the identical config and re-fail forever, starving supervision. It is
        logged loudly and the session is left supervised; the next connect
        cycle re-provisions.
        """
        self._next_feed = time.monotonic() + self._feed_period()
        self._feed_rejected_logged = False
        try:
            config = self._resolve_interface(ru_config)
            if config is None:
                return
            ru_config.set_full_config(config, skip_activation=True)
            if self._activation_state() != "ACTIVE":
                # the carriers are deliberately left down: no sync
                # precondition, no receipt to wait for
                self._feed_if_due(ru_config)
                ru_config.activate_full_config(config)
                logging.info("O-RU provisioned; carriers written INACTIVE by configuration")
                return
            if not self._wait_for_sync_locked(ru_config):
                logging.warning(
                    "O-RU not sync-LOCKED within %ss; provisioned but carriers left inactive", self.sync_timeout_s
                )
                return
            # the activation edit may itself block for the RPC timeout
            self._feed_if_due(ru_config)
            ru_config.activate_full_config(config)
            states = self._wait_for_carriers_ready(ru_config)
            receipt = ", ".join(f"{name}={state}" for name, state in sorted(states.items())) or "none exposed"
            if states and all(state == "READY" for state in states.values()):
                logging.info("O-RU provisioned; carrier activation receipt: %s", receipt)
            else:
                logging.warning(
                    "Carriers not all READY within %ss of activation: %s", self.carrier_ready_timeout_s, receipt
                )
        except rpc_ops.RPCError as err:
            # edit_config already logged the per-node NETCONF error detail; do
            # not re-raise — recycling on a rejected edit is a guaranteed
            # infinite loop that takes the whole M-plane session down with it.
            logging.error(
                "O-RU rejected a provisioning edit or read; leaving the session supervised rather than "
                "recycling (a reconnect would re-push and re-fail); the next connect cycle re-provisions. "
                "Detail: %s",
                err,
            )

    def _resolve_interface(self, ru_config):
        """The config to push this cycle, its processing element bound to the VLAN interface.

        sudo creates that interface itself (uc-vlan<vlan> from the interface
        section, or processing.interface_name). hybrid-odu is read-only on
        ietf-interfaces (Table 6.5-1): the interface is the management
        plane's and transport-flow/interface-name is a leafref into it, so
        the name is processing.interface_name when declared, else the l2vlan
        interface carrying processing.vlan, and the cycle waits up to
        interface_timeout_s for it to exist, feeding the watchdog. None when
        it never appeared: logged, nothing pushed, the next connect cycle
        tries again (the O-RU would reject the element and stop the cycle
        there anyway).
        """
        if ru_config.can_write("ietf-interfaces"):
            return self.provision_config
        processing = self.provision_config["processing"]
        declared = processing.get("interface_name")
        vlan = str(processing["vlan"])
        deadline = time.monotonic() + self.interface_timeout_s
        while True:
            self._feed_if_due(ru_config)
            name = self._find_vlan_interface(ru_config, declared, vlan)
            if name is not None:
                logging.info("Processing element bound to the management plane's interface %s (VLAN %s)", name, vlan)
                return {**self.provision_config, "processing": {**processing, "interface_name": name}}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logging.warning(
                    "O-RU has no %s yet (the management plane creates it for the hybrid-odu role); "
                    "waited %ss, provisioning deferred to the next connect cycle",
                    f"interface {declared}" if declared else f"l2vlan interface for VLAN {vlan}",
                    self.interface_timeout_s,
                )
                return None
            time.sleep(min(self.sync_poll_s, remaining))

    @staticmethod
    def _find_vlan_interface(ru_config, declared, vlan):
        """Name of the interface the processing element should reference, or None.

        A strict read: a dead session raises and recycles. Several interfaces
        on the VLAN would be an ambiguous profile — the conventional
        uc-vlan<vlan> wins when present, else the first by name, with a
        warning.
        """
        data = ru_config.get_ietf_interfaces(strict=True) or {}
        entries = [
            entry for entry in ensure_list((data.get("interfaces") or {}).get("interface")) if isinstance(entry, dict)
        ]
        names = [str(entry["name"]) for entry in entries if entry.get("name") is not None]
        if declared is not None:
            return declared if declared in names else None
        candidates = sorted(
            str(entry["name"])
            for entry in entries
            if entry.get("name") is not None and str(entry.get("vlan-id")) == vlan
        )
        if not candidates:
            return None
        if len(candidates) > 1:
            chosen = f"uc-vlan{vlan}" if f"uc-vlan{vlan}" in candidates else candidates[0]
            logging.warning(
                "Several interfaces carry VLAN %s (%s); binding the processing element to %s",
                vlan,
                ", ".join(candidates),
                chosen,
            )
            return chosen
        return candidates[0]

    def _activation_state(self):
        return str((self.provision_config.get("activation") or {}).get("state", "ACTIVE")).upper()

    def _feed_period(self):
        return max(0.05, self.supervision_interval / 2)

    def _feed_if_due(self, ru_config):
        """Feed the watchdog when the cycle-wide schedule says so."""
        if time.monotonic() >= self._next_feed:
            self._feed_watchdog(ru_config)
            self._next_feed = time.monotonic() + self._feed_period()

    def _wait_for_sync_locked(self, ru_config):
        """Poll sync-status until LOCKED, feeding the supervision watchdog.

        The strict status read raises on a dead session; the watchdog feed
        keeps the O-RU's supervision budget from starving while waiting for
        PTP lock.
        """
        deadline = time.monotonic() + self.sync_timeout_s
        while True:
            self._feed_if_due(ru_config)
            if ru_config.get_sync_status(strict=True).get("sync_state") == "LOCKED":
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(self.sync_poll_s, remaining))

    def _wait_for_carriers_ready(self, ru_config):
        """Poll array-carriers state until every carrier is READY, feeding the watchdog.

        The activation edit is only an attempt — carrier state is asynchronous
        (DISABLED -> BUSY -> READY) and this readback is the receipt. Runs
        before the session's notification-driven watchdog loop starts, so it
        stays on the provisioner's feed schedule. Returns the final
        {carrier-name: state} map; the caller judges completeness.
        """
        return ru_config.wait_for_carriers_ready(
            timeout_s=self.carrier_ready_timeout_s,
            poll_interval_s=self.sync_poll_s,
            on_poll=lambda: self._feed_if_due(ru_config),
            strict=True,
        )

    def _feed_watchdog(self, ru_config):
        """Reset the supervision watchdog; rpc-error is alive-but-rejected.

        A rejected reset during a long wait means the O-RU's watchdog is not
        being fed at all, so the first one in a cycle is a warning; the rest
        are debug noise.
        """
        try:
            ru_config.reset_supervision_watchdog(self.supervision_interval, self.supervision_guard)
        except rpc_ops.RPCError as err:
            if self._feed_rejected_logged:
                logging.debug("supervision-watchdog-reset rejected during provisioning: %s", err)
            else:
                self._feed_rejected_logged = True
                logging.warning("supervision-watchdog-reset rejected during provisioning: %s", err)


# tdd keys that steer the provisioning steps rather than describe a pattern;
# anything else in the section is pattern content (the rule set_full_config
# applies), so a typo there must fail at load rather than on every cycle
_TDD_CONTROL_KEYS = frozenset(
    ("pattern_upload", "carrier_binding", "nof_tx_carriers", "nof_rx_carriers", "tdd_pattern_id")
)

_REQUIRED_LEAVES = {
    "interface": ("ru_mac_addr", "vlan"),
    "processing": ("ru_mac_addr", "du_mac_addr", "vlan"),
    "endpoint": ("num_prb", "frame_structure", "iq_bitwidth", "compression_type"),
    "carrier": ("dl_arfcn", "dl_freq", "ul_arfcn", "ul_freq", "tx_gain", "rf_bandwidth_hz"),
}

_PORT_LIST_KEYS = ("dl_port_id", "ul_port_id", "prach_port_id")


def _require_bool(section, key, where, path):
    """A provisioning switch must be a YAML boolean: a quoted "false" is truthy."""
    value = section.get(key)
    if value is not None and not isinstance(value, bool):
        raise ValueError(f"provision config {path}: {where}.{key} must be true or false, not {value!r}")


def _check_sections(config, path):
    """Required sections and leaves; the templates render a missing leaf as an
    empty element the O-RU rejects on every connect cycle."""
    for key, leaves in _REQUIRED_LEAVES.items():
        section = config.get(key)
        if not isinstance(section, dict) or not section:
            raise ValueError(f"provision config {path}: section '{key}' must be a non-empty mapping")
        missing = [leaf for leaf in leaves if section.get(leaf) is None]
        if missing:
            raise ValueError(f"provision config {path}: {key} is missing {', '.join(missing)}")
    # the processing element names the VLAN interface — the one the interface
    # section creates (uc-vlan<vlan>) unless processing.interface_name names
    # the management plane's — and carries the same O-RU MAC address
    name = config["processing"].get("interface_name")
    if name is not None and (not isinstance(name, str) or not name.strip()):
        raise ValueError(f"provision config {path}: processing.interface_name must be a non-empty string")
    # the port's L2 MTU is per deployment (jumbo frames are optional on the fronthaul); the
    # template defaults it, the leaf's own range is o-ran-interfaces' 64..65535
    mtu = config["interface"].get("l2_mtu")
    if mtu is not None and (isinstance(mtu, bool) or not isinstance(mtu, int) or not 64 <= mtu <= 65535):
        raise ValueError(f"provision config {path}: interface.l2_mtu must be an integer in 64..65535, not {mtu!r}")
    for leaf in ("vlan", "ru_mac_addr"):
        if config["interface"][leaf] != config["processing"][leaf]:
            raise ValueError(f"provision config {path}: interface.{leaf} and processing.{leaf} must match")
    for key in _PORT_LIST_KEYS:
        ports = config["endpoint"].get(key)
        if ports is not None and (not isinstance(ports, list) or not all(isinstance(port, int) for port in ports)):
            raise ValueError(f"provision config {path}: endpoint.{key} must be a list of integers")
    tdd = config.get("tdd")
    if tdd is not None and not isinstance(tdd, dict):
        raise ValueError(f"provision config {path}: tdd must be a mapping when present")
    for key in ("pattern_upload", "carrier_binding"):
        _require_bool(tdd or {}, key, "tdd", path)
    pm = config.get("pm")
    if pm is not None and not isinstance(pm, dict):
        raise ValueError(f"provision config {path}: pm must be a mapping when present")


def _normalize_declarations(config, path):
    """Normalize the explicit endpoint/switching-point declarations in place.

    Both are device-facing lists that would otherwise fail on every connect
    cycle; normalizing at load makes a malformed declaration a startup
    error. The generated-name knobs are exercised the same way, and a tdd
    section carrying pattern content is a complete spec (see
    set_full_config), so it is computed once here to prove it coherent.
    """
    try:
        endpoint_naming(config["endpoint"])
    except (TypeError, ValueError) as err:
        raise ValueError(f"provision config {path}: endpoint naming: {err}") from err
    for key in ("tx_endpoints", "rx_endpoints", "prach_endpoints"):
        if config["endpoint"].get(key) is not None:
            try:
                config["endpoint"][key] = normalize_endpoint_entries(config["endpoint"][key], f"endpoint.{key}")
            except ValueError as err:
                raise ValueError(f"provision config {path}: {err}") from err
    tdd = config.get("tdd") or {}
    if tdd.get("switching_points") is not None:
        try:
            tdd["switching_points"] = validate_switching_points(tdd["switching_points"])
        except (TypeError, ValueError) as err:
            raise ValueError(f"provision config {path}: tdd.switching_points: {err}") from err
    elif tdd.keys() - _TDD_CONTROL_KEYS:
        try:
            compute_tdd_switching_points(
                tdd["scs_khz"],
                tdd["dl_ul_tx_period"],
                tdd["nof_dl_slots"],
                nof_dl_symbols=tdd.get("nof_dl_symbols", 0),
                nof_ul_symbols=tdd.get("nof_ul_symbols", 0),
                nof_ul_slots=tdd.get("nof_ul_slots"),
            )
        except KeyError as err:
            raise ValueError(
                f"provision config {path}: tdd pattern needs scs_khz, dl_ul_tx_period and nof_dl_slots "
                f"(missing {err})"
            ) from err
        except (TypeError, ValueError) as err:
            raise ValueError(f"provision config {path}: tdd: {err}") from err


def _check_activation(config, path):
    """Default and validate the activation block in place."""
    activation = config.get("activation") or {}
    if not isinstance(activation, dict):
        raise ValueError(f"provision config {path}: activation must be a mapping")
    # Validate the enum here too — the CLI path enforces choices=(ACTIVE,INACTIVE)
    # via argparse, but a YAML typo would otherwise reach the O-RU as an
    # out-of-range <active> value and get rpc-error'd on every connect cycle.
    state = str(activation.get("state", "ACTIVE")).upper()
    if state not in ("ACTIVE", "INACTIVE"):
        raise ValueError(
            f"provision config {path}: activation.state must be ACTIVE or INACTIVE, not {activation['state']!r}"
        )
    activation["state"] = state
    _require_bool(activation, "tolerate_reply_timeout", "activation", path)
    # Provisioning re-applies activation on every connect cycle and the
    # array-carriers state readback (not the edit reply) is the activation
    # receipt; some O-RUs accept the activation edit but never reply once the
    # carriers are already active. Tolerate a missing reply by default so a
    # benign re-activation cannot recycle the session (a strict readback still
    # catches a genuinely dead one). Set false explicitly to opt out.
    activation.setdefault("tolerate_reply_timeout", True)
    config["activation"] = activation


def load_provision_config(path, loader):
    """Load and validate a full-config YAML for provisioning.

    loader parses the open file (yaml.safe_load; injected so the module has
    no YAML dependency of its own and tests can feed it directly).
    Validation is deliberately fail-fast: every required section and leaf
    must be present, the provisioning switches must be booleans, explicit
    endpoint/switching-point declarations are normalized, and the activation
    block defaults to {"state": "ACTIVE"} — malformed files error at
    startup, not on every connect cycle.
    """
    with open(path, encoding="utf-8") as handle:
        config = loader(handle)
    if not isinstance(config, dict):
        raise ValueError(f"provision config {path} must be a mapping")
    _check_sections(config, path)
    _normalize_declarations(config, path)
    _check_activation(config, path)
    return config
