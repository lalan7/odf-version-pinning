#!/usr/bin/env python3
"""Filter a rendered OPM catalog to pin ODF to a specific z-stream version.

Usage:
    # Render the full catalog first:
    opm render registry.redhat.io/redhat/redhat-operator-index:v4.22 \
        > catalog/full-index.json

    # Filter to a specific version (CLI args):
    python3 scripts/filter-catalog.py --version v4.21.2-rhodf --channel stable-4.21

    # Or use environment variables:
    ODF_TARGET_VERSION=v4.21.5-rhodf python3 scripts/filter-catalog.py

    # Or change the catalog index version:
    python3 scripts/filter-catalog.py --version v4.22.0-rhodf --channel stable-4.22

Reads:   catalog/full-index.json  (pretty-printed JSON stream from opm render)
Writes:  fbc/index.json           (NDJSON suitable for opm validate / podman build)

The script auto-discovers ODF sub-operators by scanning the catalog for all
bundles that share the same version suffix (e.g., 4.21.2-rhodf) and belong to
packages with a matching channel. This means new sub-operators added in future
ODF releases are picked up automatically without editing this script.

Note: The full Red Hat catalog is ~105 MB. This script loads it entirely into
memory (~300 MB peak). Ensure sufficient RAM on the host running the filter.
"""

import argparse
import json
import os
import sys


def parse_args():
    parser = argparse.ArgumentParser(
        description="Filter an OPM catalog to pin ODF to a specific z-stream version."
    )
    parser.add_argument(
        "--version",
        default=os.environ.get("ODF_TARGET_VERSION", "v4.21.2-rhodf"),
        help="ODF version suffix to pin (default: $ODF_TARGET_VERSION or v4.21.2-rhodf)",
    )
    parser.add_argument(
        "--channel",
        default=os.environ.get("ODF_TARGET_CHANNEL", "stable-4.21"),
        help="OLM channel name (default: $ODF_TARGET_CHANNEL or stable-4.21)",
    )
    parser.add_argument(
        "--input",
        default=os.environ.get("CATALOG_INPUT", "catalog/full-index.json"),
        help="Path to rendered catalog JSON (default: catalog/full-index.json)",
    )
    parser.add_argument(
        "--output",
        default=os.environ.get("CATALOG_OUTPUT", "fbc/index.json"),
        help="Path to write pruned FBC (default: fbc/index.json)",
    )
    return parser.parse_args()


_WHITESPACE = frozenset(" \t\n\r")


def parse_catalog(data):
    """Parse a pretty-printed JSON stream into a list of objects."""
    decoder = json.JSONDecoder()
    idx = 0
    length = len(data)
    objects = []
    while idx < length:
        while idx < length and data[idx] in _WHITESPACE:
            idx += 1
        if idx >= length:
            break
        obj, end_idx = decoder.raw_decode(data, idx)
        idx = end_idx
        objects.append(obj)
    return objects


def discover_odf_packages(objects, target_suffix):
    """Find all packages that have a bundle ending with the target suffix.

    Instead of hardcoding package names, we look for any bundle whose name
    ends with the version suffix (e.g., '.v4.21.2-rhodf'). This catches
    new sub-operators added in future ODF releases.
    """
    packages = set()
    for obj in objects:
        if obj.get("schema") == "olm.bundle":
            name = obj.get("name", "")
            if name.endswith("." + target_suffix):
                packages.add(obj.get("package", ""))
    return packages


def main():
    args = parse_args()

    print(f"Target version: {args.version}")
    print(f"Target channel: {args.channel}")
    print(f"Reading catalog from {args.input}...")

    with open(args.input) as f:
        data = f.read()
    objects = parse_catalog(data)
    print(f"Parsed {len(objects)} catalog objects")

    odf_packages = discover_odf_packages(objects, args.version)
    if not odf_packages:
        print(f"ERROR: No packages found with bundles matching '*.{args.version}'",
              file=sys.stderr)
        print("Available bundle suffixes (sample):", file=sys.stderr)
        seen = set()
        for obj in objects:
            if obj.get("schema") == "olm.bundle":
                name = obj.get("name", "")
                suffix = name.split(".", 1)[1] if "." in name else name
                if suffix not in seen and len(seen) < 10:
                    seen.add(suffix)
                    print(f"  - {suffix}", file=sys.stderr)
        sys.exit(1)

    print(f"Discovered {len(odf_packages)} ODF packages:")
    for p in sorted(odf_packages):
        print(f"  - {p}")

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    kept = 0

    with open(args.output, "w") as fout:
        for obj in objects:
            schema = obj.get("schema", "")

            if schema == "olm.package":
                if obj["name"] in odf_packages:
                    obj["defaultChannel"] = args.channel
                    fout.write(json.dumps(obj, separators=(",", ":")) + "\n")
                    kept += 1

            elif schema == "olm.channel":
                pkg = obj.get("package", "")
                if pkg in odf_packages and obj.get("name") == args.channel:
                    target_bundle = pkg + "." + args.version
                    obj["entries"] = [
                        e
                        for e in obj.get("entries", [])
                        if e.get("name") == target_bundle
                    ]
                    if obj["entries"]:
                        fout.write(json.dumps(obj, separators=(",", ":")) + "\n")
                        kept += 1

            elif schema == "olm.bundle":
                pkg = obj.get("package", "")
                if pkg in odf_packages:
                    target_bundle = pkg + "." + args.version
                    if obj.get("name") == target_bundle:
                        fout.write(json.dumps(obj, separators=(",", ":")) + "\n")
                        kept += 1

    expected = len(odf_packages) * 3
    print(f"\nKept {kept} entries (expected {expected} = "
          f"{len(odf_packages)} packages + {len(odf_packages)} channels + "
          f"{len(odf_packages)} bundles)")
    print(f"Written to {args.output}")

    if kept != expected:
        print(f"ERROR: expected {expected} but got {kept}. "
              "Some packages may not have a matching channel or bundle.",
              file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
