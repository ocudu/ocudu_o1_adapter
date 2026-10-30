# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-FileCopyrightText: Copyright (C) 2026 OCUDU contributors
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Passwords read from files instead of the command line (the *_password_file options)."""

# Each password option with a *_file alternative: a secret on the command line is
# readable by every user of the host (ps, /proc/<pid>/cmdline); a file, such as a
# mounted Kubernetes Secret, is not.
PASSWORD_FILE_OPTIONS = ("netconf_password", "ru_netconf_password", "ves_password")


def read_password_files(arg_parser, args):
    """Replace each --<option>_file given by the password it holds, or exit
    through arg_parser.error.

    The file's content is the password, with trailing line breaks removed (a
    file written by echo ends in one). Giving both --<option> and
    --<option>_file is a usage error, as is a missing, unreadable or empty file.
    """
    for option in PASSWORD_FILE_OPTIONS:
        path = getattr(args, f"{option}_file")
        if not path:
            continue
        if getattr(args, option) != arg_parser.get_default(option):
            arg_parser.error(f"--{option} and --{option}_file cannot be combined")
        try:
            with open(path, encoding="utf-8") as handle:
                password = handle.read().rstrip("\r\n")
        except OSError as err:
            arg_parser.error(f"--{option}_file: {err}")
        if not password:
            arg_parser.error(f"--{option}_file: {path} is empty")
        setattr(args, option, password)
