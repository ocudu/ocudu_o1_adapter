# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-FileCopyrightText: Copyright (C) 2026 OCUDU contributors
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI
"""Render a gNB/CU/DU YAML config from a NETCONF (3GPP NRM) XML config, offline.

The datastore -> YAML direction the adapter runs at every config change, as a command: the same
template context (config_manager.build_template_context), fed from a config file instead of a
get-config. With yaml_to_netconf this closes the loop a seed can be checked with: YAML -> XML ->
YAML must reproduce what went in.

The input is the server's config as yaml_to_netconf writes it (a <ManagedElement> document), a
get-config reply (<data><ManagedElement>) or an edit-config payload (<config><ManagedElement>).
"""

import argparse
import logging
import sys
from pathlib import Path

import xmltodict

from config_manager import render_yaml

_PROFILES = ("gnb", "cu", "cucp", "cuup", "du")
_TEMPLATES = Path(__file__).resolve().parent.parent / "templates"


def load_raw_config(xml_text):
    """Parse a NETCONF config document into the get-config dict shape the extractors read."""
    doc = xmltodict.parse(xml_text)
    for wrapper in ("rpc-reply", "data", "config"):
        if wrapper in doc:
            doc = doc[wrapper]
    if "ManagedElement" not in doc:
        raise ValueError("the XML carries no ManagedElement")
    return {"data": {"ManagedElement": doc["ManagedElement"]}}


def main(argv=None):
    """CLI entry point: render the YAML config of one profile from a NETCONF XML config."""
    parser = argparse.ArgumentParser(description="Render a gNB/CU/DU YAML config from a NETCONF XML config.")
    parser.add_argument("-i", "--input", required=True, help="NETCONF XML config (ManagedElement, data or config root)")
    parser.add_argument("-p", "--profile", required=True, choices=_PROFILES, help="The split to render")
    parser.add_argument("-t", "--template", help="Template file name (default: <profile>.yaml)")
    parser.add_argument("-o", "--output", help="Output YAML file (default: stdout)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Log extraction warnings")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.ERROR, format="%(levelname)s: %(message)s")

    with open(args.input, encoding="utf-8") as handle:
        try:
            raw_config = load_raw_config(handle.read())
        except ValueError as e:
            logging.error("%s", e)
            return 1

    content = render_yaml(raw_config, args.profile, args.template or f"{args.profile}.yaml", str(_TEMPLATES))
    if args.output:
        with open(args.output, mode="w", encoding="utf-8") as handle:
            handle.write(content)
    else:
        sys.stdout.write(content)
    return 0


if __name__ == "__main__":
    sys.exit(main())
