# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""
Restricts the SSH algorithms ncclient offers to the set the ocudu_netconf server accepts.

ncclient builds its own ``paramiko.Transport`` internally and exposes no way to pass
transport parameters through ``manager.connect()``, so the only hook is paramiko's
class-level defaults. They are read via ``getattr(self, "_preferred_*")`` when the
transport is constructed, so overwriting them here applies to every session opened
afterwards - the netopeer2 one as well as the RU ones.

The lists below mirror the KEXINIT proposal of the netopeer2/libssh server in
ocudu_netconf, minus the algorithms paramiko does not implement:
  - chacha20-poly1305@openssh.com (cipher)
  - curve25519-sha256, the alias of curve25519-sha256@libssh.org (kex)
  - diffie-hellman-group18-sha512 (kex)
Host key algorithms are deliberately left at paramiko's defaults: the server only
offers rsa-sha2-512/rsa-sha2-256, but pinning those would lock out RUs presenting
an ed25519 or ecdsa host key.
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

PREFERRED_KEX = (
    "curve25519-sha256@libssh.org",
    "ecdh-sha2-nistp256",
    "ecdh-sha2-nistp384",
    "ecdh-sha2-nistp521",
    "diffie-hellman-group16-sha512",
    "diffie-hellman-group-exchange-sha256",
    "diffie-hellman-group14-sha256",
)


def restrict_ssh_algorithms() -> None:
    """Narrow paramiko's offered algorithms. Call once at startup, before connecting."""
    # pylint: disable=protected-access
    paramiko.Transport._preferred_ciphers = PREFERRED_CIPHERS  # type: ignore[attr-defined]
    paramiko.Transport._preferred_macs = PREFERRED_MACS  # type: ignore[attr-defined]
    paramiko.Transport._preferred_kex = PREFERRED_KEX  # type: ignore[attr-defined]
    logging.debug("SSH ciphers restricted to %s", ", ".join(PREFERRED_CIPHERS))
