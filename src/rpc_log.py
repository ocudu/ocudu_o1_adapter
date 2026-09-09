# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-FileCopyrightText: Copyright (C) 2026 OCUDU contributors
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""NETCONF conversation capture ("RPC flight recorder").

ncclient logs every message it sends and receives — rpc, rpc-reply and
notification XML — at DEBUG level on loggers under the ``ncclient``
namespace. Both entry points normally pin that namespace to WARNING to keep
the console readable, which also makes the on-the-wire conversation
invisible. For bring-up and debugging against a real O-RU the raw exchange
is the primary evidence, so this module redirects it to a file without
changing what reaches the console.
"""

import argparse
import logging
from typing import Dict

_CONSOLE_FORMAT = "%(asctime)s \x1b[32;20m[%(levelname)s]\x1b[0m %(message)s"
_FILE_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

# Console handler installed alongside the capture, so disable can remove
# exactly what enable added and nothing else. One capture at a time: while it
# is active a second enable returns the existing file handler.
_console_siblings: Dict[logging.FileHandler, logging.Handler] = {}


def add_rpc_log_argument(parser: argparse.ArgumentParser) -> None:
    """Register the shared ``--rpc_log`` CLI flag on ``parser``."""
    parser.add_argument(
        "--rpc_log",
        type=str,
        default=None,
        metavar="FILE",
        help="Append the raw NETCONF conversation (every rpc, rpc-reply and notification) to FILE",
    )


def enable_rpc_log(path: str) -> logging.FileHandler:
    """Capture the full NETCONF conversation to ``path``.

    Raises the ``ncclient`` logger to DEBUG with a dedicated file handler and
    stops propagation to the root logger, replacing it with a WARNING-level
    console handler so terminal output stays exactly as it is without the
    capture. Idempotent: while a capture is active a second call returns its
    handler and installs nothing. Returns the file handler so callers (tests)
    can detach it via :func:`disable_rpc_log`.
    """
    if _console_siblings:
        return next(iter(_console_siblings))
    logger = logging.getLogger("ncclient")
    file_handler = logging.FileHandler(path)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(_FILE_FORMAT))
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.WARNING)
    console_handler.setFormatter(logging.Formatter(_CONSOLE_FORMAT))
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    _console_siblings[file_handler] = console_handler
    return file_handler


def disable_rpc_log(file_handler: logging.FileHandler) -> None:
    """Undo :func:`enable_rpc_log` for ``file_handler``.

    Detaches the handler and its console sibling. The WARNING pin and
    propagation are restored only when that removed the last capture this
    module installed, so a stale handle cannot silence a live capture; a
    handle this module never installed is ignored.
    """
    console_handler = _console_siblings.pop(file_handler, None)
    if console_handler is None:
        return
    logger = logging.getLogger("ncclient")
    for handler in (file_handler, console_handler):
        logger.removeHandler(handler)
        handler.close()
    if not _console_siblings:
        logger.setLevel(logging.WARNING)
        logger.propagate = True
