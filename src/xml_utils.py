# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Shared helpers for normalising xmltodict/NETCONF payload values."""

from typing import Any, List


def ensure_list(value: Any) -> List[Any]:
    """Normalise xmltodict output: missing -> [], single -> [x], list -> list."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]
