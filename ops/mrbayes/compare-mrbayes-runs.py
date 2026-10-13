#!/usr/bin/env python3
"""compare-mrbayes-runs.py <dirA> <dirB> [-q]

Compare two MrBayes run directories file by file (every output file plus
stdout.txt) and exit 0 only if they are identical apart from what legitimately
differs between two runs of the same analysis:

  - the run ID stamp "[ID: n]" and "MCMC stamp = n" (drawn from the clock);
  - "Seed (for generating default start values) = n" (the clock seed that
    `set seed` then replaces);
  - the remaining-time column of progress lines and the run/CPU time totals;
  - the MrBayes and BEAGLE version banners and BEAGLE's resource name.

Nothing else is normalized: sampled parameters, trees, likelihoods, split
frequencies, convergence diagnostics, checkpoints and summaries must match
byte for byte.
"""
import argparse
import os
import re
import sys

NORMALIZE = [
    (re.compile(r'\[ID: \d+\]'), '[ID: X]'),
    (re.compile(r'MCMC stamp = \d+'), 'MCMC stamp = X'),
    (re.compile(r'Seed \(for generating default start values\) = \d+'), 'Seed (default start values) = X'),
    (re.compile(r'( -- )\d+:\d\d:\d\d$'), r'\1T'),
    (re.compile(r'Analysis completed in .*'), 'Analysis completed in T'),
    (re.compile(r'Analysis used [0-9.]+ seconds of CPU time'), 'Analysis used T seconds of CPU time'),
    (re.compile(r'^(\s*MrBayes) \S+ (\S+)$'), r'\1 VERSION \2'),
    (re.compile(r'^(\s*Version:\s+)\S+'), r'\1VERSION'),
    (re.compile(r'Using BEAGLE v\S+( \(PRE-RELEASE\))? resource'), 'Using BEAGLE vX resource'),
    (re.compile(r'(Rsrc Name : )\S+( \(\S+\))?'), r'\1X'),
]


def normalized(path):
    with open(path, 'rb') as f:
        text = f.read().decode('latin-1')
    out = []
    for line in text.split('\n'):
        for rx, rep in NORMALIZE:
            line = rx.sub(rep, line)
        out.append(line)
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Compare two MrBayes run directories (see the module docstring).')
    parser.add_argument('dir_a')
    parser.add_argument('dir_b')
    parser.add_argument('-q', '--quiet', action='store_true',
                        help='print only the differences')
    args = parser.parse_args(argv)
    a, b, quiet = args.dir_a, args.dir_b, args.quiet
    names_a = sorted(f for f in os.listdir(a) if not f.startswith('.'))
    names_b = sorted(f for f in os.listdir(b) if not f.startswith('.'))
    bad = 0
    if names_a != names_b:
        print('different files:', sorted(set(names_a) ^ set(names_b)))
        bad += 1
    for name in names_a:
        if name not in names_b:
            continue
        la, lb = normalized(os.path.join(a, name)), normalized(os.path.join(b, name))
        if la == lb:
            if not quiet:
                print('same', name)
            continue
        bad += 1
        for i, (x, y) in enumerate(zip(la, lb)):
            if x != y:
                print('DIFF %s line %d:\n  < %s\n  > %s' % (name, i + 1, x[:200], y[:200]))
                break
        else:
            print('DIFF %s: %d vs %d lines' % (name, len(la), len(lb)))
    if bad and not quiet:
        print('MISMATCH (%d files)' % bad)
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
