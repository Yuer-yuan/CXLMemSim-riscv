#!/usr/bin/env python3
"""Apply the pinned IOR rand/srand portability fix without changing geometry."""
import argparse
import hashlib
import json
from pathlib import Path


def patch_source(text):
    for function in ("static int init_random_seed(", "IOR_offset_t *GetOffsetArrayRandom("):
        if function not in text:
            raise ValueError("unrecognized IOR random-offset implementation")
    old, new = "srandom(seed);", "srand(seed);"
    if text.count(old) == 2 and text.count(new) == 0:
        # Both count and fill passes call rand(). glibc aliases the two PRNG
        # families, but musl does not: srandom cannot reset the rand stream.
        return text.replace(old, new)
    if text.count(old) == 0 and text.count(new) == 2:
        return text
    raise ValueError("IOR seed sites differ from the reviewed pinned source")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()
    before = args.source.read_text()
    after = patch_source(before)
    if after != before:
        args.source.write_text(after)
    print(json.dumps(dict(fix="ior-rand-srand", changed=before != after,
                          sha256=hashlib.sha256(after.encode()).hexdigest())))


if __name__ == "__main__":
    main()
