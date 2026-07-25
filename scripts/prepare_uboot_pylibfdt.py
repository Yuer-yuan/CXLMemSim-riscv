#!/usr/bin/env python3
"""Adapt pinned U-Boot's pylibfdt typemaps for current SWIG."""

import argparse
import pathlib


LEGACY_CALL = "SWIG_Python_AppendOutput(resultobj, "
PORTABLE_CALL = "SWIG_AppendOutput(resultobj, "
EXPECTED_CALLS = 3


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args()

    source = args.source.read_text(encoding="utf-8")
    count = source.count(LEGACY_CALL)
    if count != EXPECTED_CALLS:
        parser.error(
            f"expected {EXPECTED_CALLS} legacy SWIG calls in {args.source}, "
            f"found {count}"
        )

    adapted = source.replace(LEGACY_CALL, PORTABLE_CALL)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(adapted, encoding="utf-8")


if __name__ == "__main__":
    main()
