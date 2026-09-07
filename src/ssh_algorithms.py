# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""
Restricts the SSH algorithms ncclient offers to the set the ocudu_netconf server accepts.

ncclient builds its transport as ``paramiko.Transport(sock)`` and exposes no way to pass transport
parameters through ``manager.connect()``. That call is an attribute lookup on the paramiko module,
so rebinding the name to the subclass below is enough: every session ncclient opens afterwards -
the netopeer2 one as well as the RU ones - is built from it, while paramiko's own class keeps its
defaults for anything else in the process.

paramiko documents ``disabled_algorithms`` for this, but it only subtracts from the built-in lists
and cannot reorder them. Since the client's preference order decides the winner, that would leave
aes128-ctr ahead of the AEAD ciphers and plain HMAC ahead of encrypt-then-MAC. Overriding the
preference lists keeps our order; the KEXINIT is built from them through the ``preferred_ciphers``
and ``preferred_macs`` properties.

The lists mirror the KEXINIT proposal of the netopeer2/libssh server in ocudu_netconf, minus
chacha20-poly1305@openssh.com, which paramiko does not implement. Key exchange is left at
paramiko's defaults: those already match the server proposal, and spelling out curve25519 here
would break the handshake on a paramiko build without it. Host key algorithms are left alone as
well - the server only offers rsa-sha2-512/rsa-sha2-256, but pinning those would lock out RUs
presenting an ed25519 or ecdsa host key.
"""

import logging

import paramiko

# Ordered as the server proposes them, strongest first.
PREFERRED_CIPHERS = (
    "aes256-gcm@openssh.com",
    "aes128-gcm@openssh.com",
    "aes256-ctr",
    "aes192-ctr",
    "aes128-ctr",
)

PREFERRED_MACS = (
    "hmac-sha2-256-etm@openssh.com",
    "hmac-sha2-512-etm@openssh.com",
    "hmac-sha2-256",
    "hmac-sha2-512",
)


class RestrictedTransport(paramiko.Transport):
    """A ``paramiko.Transport`` proposing only the cipher suites and MACs our servers accept."""

    _preferred_ciphers = PREFERRED_CIPHERS
    _preferred_macs = PREFERRED_MACS


def restrict_ssh_algorithms() -> None:
    """Make ncclient build ``RestrictedTransport``. Call once at startup, before connecting."""
    paramiko.Transport = RestrictedTransport  # type: ignore[misc]
    logging.debug("SSH ciphers restricted to %s", ", ".join(PREFERRED_CIPHERS))
