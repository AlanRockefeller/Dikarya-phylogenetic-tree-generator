#!/bin/bash
# Build and install MAFFT 7.526-opt5-dikarya1 next to the existing opt5 build.
#
#   sudo bash ops/mafft/install-mafft-dikarya.sh
#
# Clones the opt5 fork at the exact commit this patch was written against, applies
# ops/mafft/0001-mafft-dikarya1-avx2.patch, builds with the same flags the
# benchmarks used, and installs into $PREFIX.  Nothing in Dikarya changes until
# MAFFT_BINARY in /etc/dikarya/dikarya.environment.live points at the new prefix
# (see ops/mafft/README.md).  Re-running it rebuilds from scratch.
#
# The build uses -march=native: the binaries are tuned for (and only guaranteed to
# run on) the CPU they were built on, so build on the host that will run them.
set -euo pipefail

PREFIX=${PREFIX:-/opt/mafft-7.526-opt5-dikarya1}
SRC=${SRC:-/usr/local/src/mafft-opt5-dikarya1}
UPSTREAM=https://github.com/joshuaowalker/mafft
BASE=7f1af7b                                   # v7.526-opt5
REFERENCE=${REFERENCE:-/opt/mafft-7.526-opt5/bin/mafft}
HERE=$(cd "$(dirname "$0")" && pwd)
PATCH=$HERE/0001-mafft-dikarya1-avx2.patch

if [ "$(id -u)" -ne 0 ]; then echo "run as root" >&2; exit 77; fi
grep -qw avx2 /proc/cpuinfo || { echo "this CPU has no AVX2; the patch would compile to the opt5 code paths only" >&2; exit 1; }
[ -f "$PATCH" ] || { echo "missing $PATCH" >&2; exit 1; }
# The identity self-test below is what makes this build safe to switch to, so a
# missing reference stops the install rather than skipping the check.
# SKIP_SELFTEST=1 installs an unchecked build, on purpose only.
SKIP_SELFTEST=${SKIP_SELFTEST:-0}
if [ ! -x "$REFERENCE" ] && [ "$SKIP_SELFTEST" != 1 ]; then
    echo "no reference build at $REFERENCE (set REFERENCE=..., or SKIP_SELFTEST=1 to install unchecked)" >&2
    exit 1
fi

rm -rf "$SRC"
git clone --quiet "$UPSTREAM" "$SRC"
git -C "$SRC" checkout --quiet --detach "$BASE"
git -C "$SRC" apply --index "$PATCH"

cd "$SRC/core"
make clean >/dev/null
make -j"$(nproc)" CC=gcc CFLAGS="-O3 -march=native" PREFIX="$PREFIX" >"$SRC/build.log" 2>&1 \
    || { tail -20 "$SRC/build.log"; exit 1; }
make install PREFIX="$PREFIX" >>"$SRC/build.log" 2>&1

echo "installed: $("$PREFIX/bin/mafft" --version 2>&1)"

# Self-test: the new build must produce byte-identical alignments to the
# reference build at --thread 1, on MAFFT's own bundled test inputs.
if [ -x "$REFERENCE" ]; then
    tmp=$(mktemp -d)
    trap 'rm -rf "$tmp"' EXIT
    status=0
    # A synthetic DNA family (MAFFT's bundled samples are protein): one random
    # ~650 bp ancestor, 120 copies with substitutions and short indels.  Fixed
    # seed, so both builds see the same file.
    awk 'BEGIN { srand( 1 ); split( "ACGT", b, "" );
                 for( i=0; i<650; i++ ) anc = anc b[int( rand()*4 )+1];
                 for( s=1; s<=120; s++ ) {
                     out = "";
                     for( i=1; i<=length( anc ); i++ ) {
                         r = rand();
                         if( r < 0.004 ) continue;
                         c = ( r < 0.06 ) ? b[int( rand()*4 )+1] : substr( anc, i, 1 );
                         out = out c;
                         if( rand() < 0.004 ) out = out b[int( rand()*4 )+1] b[int( rand()*4 )+1];
                     }
                     printf( ">seq%03d\n%s\n", s, out );
                 } }' > "$tmp/dna.fasta"
    for f in "$tmp/dna.fasta" "$SRC"/test/sample; do
        [ -f "$f" ] || continue
        for opts in "--auto" "--localpair --maxiterate 1000" "--retree 2 --maxiterate 2"; do
            # shellcheck disable=SC2086
            env -u MAFFT_BINARIES "$REFERENCE" --thread 1 --quiet $opts "$f" >"$tmp/ref" 2>/dev/null
            # shellcheck disable=SC2086
            env -u MAFFT_BINARIES "$PREFIX/bin/mafft" --thread 1 --quiet $opts "$f" >"$tmp/new" 2>/dev/null
            if [ -s "$tmp/new" ] && cmp -s "$tmp/ref" "$tmp/new"; then
                echo "self-test ok:   $(basename "$f") $opts"
            else
                echo "self-test FAIL: $(basename "$f") $opts" >&2
                status=1
            fi
        done
    done
    [ "$status" -eq 0 ] || { echo "do not switch MAFFT_BINARY to this build" >&2; exit 1; }
else
    echo "SKIP_SELFTEST=1: this build was NOT checked against $REFERENCE;" \
         "do not switch MAFFT_BINARY to it until it has been" >&2
    exit 0
fi

echo "next: set MAFFT_BINARY=$PREFIX/bin/mafft in /etc/dikarya/dikarya.environment.live"
