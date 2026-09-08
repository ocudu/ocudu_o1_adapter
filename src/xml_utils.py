# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-FileCopyrightText: Copyright (C) 2026 OCUDU contributors
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Shared helpers for NETCONF payload values: xmltodict normalisation, rpc-error rendering, capability parsing."""

import xml.etree.ElementTree as ET
from typing import Any, List, Optional
from urllib.parse import parse_qs


def ensure_list(value: Any) -> List[Any]:
    """Normalise xmltodict output: missing -> [], single -> [x], list -> list."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def extract_bad_element(rpc_error_info):
    """Extract the bad-element name from an RPCError's info field, or None.

    ncclient parses error-info with xmltodict, so info is usually a dict;
    fall back to XML parsing when it arrives as a raw string.
    """
    if not rpc_error_info:
        return None
    if isinstance(rpc_error_info, dict):
        return rpc_error_info.get("bad-element")
    if isinstance(rpc_error_info, str):
        try:
            root = ET.fromstring(rpc_error_info)
            # A found leaf <bad-element> has no children, so it is falsy — use an
            # explicit None check, not `a or b`, or the leaf gets discarded and the
            # namespaceless fallback (which won't match a namespaced element) wins.
            element = root.find(".//{*}bad-element")
            if element is None:
                element = root.find(".//bad-element")
            return element.text if element is not None else None
        except ET.ParseError:
            return None
    return None


def describe_rpc_errors(exc) -> List[str]:
    """Human-readable lines for an RPCError, including aggregate errors.

    An edit touching several invalid nodes yields one rpc-error per node;
    ncclient then raises an aggregate RPCError whose per-error attributes
    are unset (reading them raises AttributeError) — access defensively.
    """
    lines = []
    for err in getattr(exc, "errors", None) or [exc]:
        details = []
        path = getattr(err, "path", None)
        if path:
            details.append(f"path: {path.strip()}")
        bad_element = extract_bad_element(getattr(err, "info", None))
        if bad_element:
            details.append(f"bad-element: {bad_element}")
        message = getattr(err, "message", None) or getattr(err, "tag", None) or str(err)
        lines.append(f"{message} — {', '.join(details)}" if details else str(message))
    return lines


# RFC 6243 with-defaults capability; the URI query string carries the server's
# basic-mode and, optionally, the also-supported modes
WITH_DEFAULTS_CAPABILITY = "urn:ietf:params:netconf:capability:with-defaults:1.0"


def with_defaults_mode(server_capabilities, wanted="report-all") -> Optional[str]:
    """`wanted` when the server's RFC 6243 with-defaults capability lists that mode, else None.

    ncclient validates a requested with-defaults mode against the advertised
    capability before sending anything (WithDefaultsError otherwise), so a
    mode is only requested when it is the basic-mode or among the
    also-supported modes of the capability URI's query string.
    """
    for capability in server_capabilities or ():
        uri, _, query = str(capability).partition("?")
        if uri != WITH_DEFAULTS_CAPABILITY:
            continue
        parameters = parse_qs(query)
        modes = set(parameters.get("basic-mode", []))
        for also_supported in parameters.get("also-supported", []):
            modes.update(mode.strip() for mode in also_supported.split(","))
        return wanted if wanted in modes else None
    return None
