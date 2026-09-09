# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-FileCopyrightText: Copyright (C) 2026 OCUDU contributors
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Shared helpers for normalising xmltodict/NETCONF payload values."""

import xml.etree.ElementTree as ET
from typing import Any, List


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
