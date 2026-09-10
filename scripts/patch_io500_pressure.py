#!/usr/bin/env python3
"""Opt-in fixed metadata work for the pinned, diagnostic-only IO500 payload."""
import argparse
import hashlib
import json
from pathlib import Path


EDITS = (
    ("  mdtest_generic_res res;\n", "  mdtest_generic_res res;\n  int legofs_fixed_work;\n"),
    ('static ini_option_t option[] = {\n',
     'static ini_option_t option[] = {\n'
     '  {"legofs-fixed-work", "LegoFS diagnostic only: complete n files without a time cutoff", 0, INI_BOOL, "FALSE", & o.legofs_fixed_work},\n'),
    ('static void validate(void){\n\n}',
     'static void validate(void){\n'
     '  if(o.legofs_fixed_work){\n'
     '    INVALID("LegoFS fixed-work diagnostic; not submission eligible\\n");\n'
     '  }\n}'),
    ('  u_argv_push(argv, "-W");\t/* deadline for stonewall in seconds */\n'
     '  u_argv_push_printf(argv, "%d", opt.stonewall);',
     '  if(!o.legofs_fixed_work){\n'
     '    u_argv_push(argv, "-W");\t/* deadline for stonewall in seconds */\n'
     '    u_argv_push_printf(argv, "%d", opt.stonewall);\n'
     '  }'),
)


def patch_source(text):
    if 'mdtest_easy_add_params(argv);' not in text or '"mdtest-easy-write"' not in text:
        raise ValueError("unrecognized pinned mdtest-easy-write source")
    if all(text.count(new) == 1 for _, new in EDITS):
        return text
    if "legofs_fixed_work" in text:
        raise ValueError("partial fixed-work patch")
    for old, new in EDITS:
        if text.count(old) != 1:
            raise ValueError("pinned mdtest-easy-write anchor changed")
        text = text.replace(old, new, 1)
    return text


def patch_ior_easy_source(text):
    if 'ior_easy_add_params(argv, 1);' not in text or '"ior-easy-write"' not in text:
        raise ValueError("unrecognized pinned ior-easy-write source")
    edits = (
        ('  int run;\n', '  int run;\n  int legofs_fixed_work;\n'),
        ('static ini_option_t option[] = {\n',
         'static ini_option_t option[] = {\n'
         '  {"legofs-fixed-work", "LegoFS diagnostic only: complete one finite easy block", 0, INI_BOOL, "FALSE", & o.legofs_fixed_work},\n'),
        ('static void validate(void){\n\n}',
         'static void validate(void){\n'
         '  if(o.legofs_fixed_work){\n'
         '    INVALID("LegoFS fixed-work diagnostic; not submission eligible\\n");\n'
         '  }\n}'),
        ('  u_argv_push_printf(argv, "%d", opt.stonewall);',
         '  /* Finite block ends first; independent runner deadline remains 600s. */\n'
         '  u_argv_push_printf(argv, "%d", o.legofs_fixed_work ? 601 : opt.stonewall);'),
    )
    if all(text.count(new) == 1 for _, new in edits):
        return text
    if 'legofs_fixed_work' in text:
        raise ValueError("partial IOR fixed-work patch")
    for old, new in edits:
        if text.count(old) != 1:
            raise ValueError("pinned ior-easy-write anchor changed")
        text = text.replace(old, new, 1)
    return text


def patch_ior_random_source(text):
    variants = [name for name in ("ior_rnd1MB", "ior_rnd4K")
                if f"void {name}_add_params(u_argv_t * argv)" in text]
    if len(variants) != 1:
        raise ValueError("unrecognized pinned random IOR source")
    name = variants[0]
    edits = (
        (f"opt_ior_rnd {name}_o;\n",
         f"opt_ior_rnd {name}_o;\nstatic int legofs_segment_count;\n"),
        ("static ini_option_t option[] = {\n",
         'static ini_option_t option[] = {\n'
         '  {"legofs-segment-count", "Diagnostic random segment bound; not submission eligible", 0, INI_INT, "10000000", & legofs_segment_count},\n'),
        (f"static void validate(void){{\n  opt_ior_rnd d = {name}_o;",
         f"static void validate(void){{\n  opt_ior_rnd d = {name}_o;\n"
         '  if(legofs_segment_count <= 0){\n'
         '    FATAL("Diagnostic random segment count must be positive\\n");\n'
         '  }\n'
         '  if(legofs_segment_count != 10000000){\n'
         '    INVALID("LegoFS bounded random geometry; not submission eligible\\n");\n'
         '  }'),
        ('  u_argv_push_printf(argv, "-s=%d", 10000000);',
         '  u_argv_push_printf(argv, "-s=%d", legofs_segment_count);'),
    )
    if all(text.count(new) == 1 for _, new in edits):
        return text
    if "legofs_segment_count" in text:
        raise ValueError("partial random segment patch")
    for old, new in edits:
        if text.count(old) != 1:
            raise ValueError("pinned random segment anchor changed")
        text = text.replace(old, new, 1)
    return text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--ior-easy-source", type=Path)
    parser.add_argument("--ior-random-source", type=Path, action="append", default=[])
    args = parser.parse_args()
    before = args.source.read_text()
    after = patch_source(before)
    if before != after:
        args.source.write_text(after)
    print(json.dumps({"fix": "io500-diagnostic-fixed-metadata", "changed": before != after,
                      "sha256": hashlib.sha256(after.encode()).hexdigest()}))
    if args.ior_easy_source:
        before = args.ior_easy_source.read_text()
        after = patch_ior_easy_source(before)
        if before != after:
            args.ior_easy_source.write_text(after)
        print(json.dumps({"fix": "io500-diagnostic-fixed-easy-block", "changed": before != after,
                          "sha256": hashlib.sha256(after.encode()).hexdigest()}))
    for source in args.ior_random_source:
        before = source.read_text()
        after = patch_ior_random_source(before)
        if before != after:
            source.write_text(after)
        print(json.dumps({"fix": "io500-diagnostic-random-segments", "changed": before != after,
                          "sha256": hashlib.sha256(after.encode()).hexdigest()}))


if __name__ == "__main__":
    main()
