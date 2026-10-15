#!/bin/bash
# Build and install MAFFT 7.526-opt8 next to the existing dikarya1 build.
#
#   sudo bash ops/mafft/install-mafft-opt8.sh
#
# Clones the exact-speedups fork at the commit benchmarked on this host
# (2026-10-13, see README.md), builds it with the same flags as dikarya1, and
# installs into $PREFIX.  opt8 already contains the dikarya1 AVX2 work, so no
# patch is applied.  Nothing in Dikarya changes until MAFFT_BINARY in
# /etc/dikarya/dikarya.environment.live points at the new prefix.  Re-running it
# rebuilds from scratch.
#
# The build uses -march=native: the binaries are tuned for (and only guaranteed to
# run on) the CPU they were built on, so build on the host that will run them.
set -euo pipefail

PREFIX=${PREFIX:-/opt/mafft-7.526-opt8}
SRC=${SRC:-/usr/local/src/mafft-opt8}
# The same commit is on https://github.com/AlanRockefeller/mafft once that fork
# is fast-forwarded; either URL gives the same tree, because COMMIT is pinned.
UPSTREAM=${UPSTREAM:-https://github.com/joshuaowalker/mafft}
COMMIT=322527b8f0b4b6b030cd4325a8aafe7c970a5764   # v7.526-opt8
REFERENCE=${REFERENCE:-/opt/mafft-7.526-opt5-dikarya1/bin/mafft}

if [ "$(id -u)" -ne 0 ]; then echo "run as root" >&2; exit 77; fi
grep -qw avx2 /proc/cpuinfo || { echo "this CPU has no AVX2; this build was only measured on the AVX2 paths" >&2; exit 1; }
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
git -C "$SRC" checkout --quiet --detach "$COMMIT"

cd "$SRC/core"
make clean >/dev/null
make -j"$(nproc)" CC=gcc CFLAGS="-O3 -march=native" PREFIX="$PREFIX" >"$SRC/build.log" 2>&1 \
    || { tail -20 "$SRC/build.log"; exit 1; }
make install PREFIX="$PREFIX" >>"$SRC/build.log" 2>&1

echo "installed: $("$PREFIX/bin/mafft" --version 2>&1)"

if [ "$SKIP_SELFTEST" = 1 ]; then
    echo "SKIP_SELFTEST=1: this build was NOT checked against $REFERENCE;" \
         "do not switch MAFFT_BINARY to it until it has been" >&2
    exit 0
fi

# Self-test: the new build must produce byte-identical alignments to the
# reference build at --thread 1, in every mode Dikarya runs.  Takes a few minutes.
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

# Synthetic DNA families (MAFFT's bundled sample is protein), fixed seeds so both
# builds see the same files: a random ancestor, copies with substitutions and
# short indels, and every REVCOMP-th copy reverse-complemented so the direction
# checks have something to turn.
family() {  # seed nseq length revcomp_every
    awk -v seed="$1" -v n="$2" -v len="$3" -v rc="$4" '
        BEGIN { srand( seed ); split( "ACGT", b, "" );
                comp["A"] = "T"; comp["C"] = "G"; comp["G"] = "C"; comp["T"] = "A";
                for( i=0; i<len; i++ ) anc = anc b[int( rand()*4 )+1];
                for( s=1; s<=n; s++ ) {
                    out = "";
                    for( i=1; i<=length( anc ); i++ ) {
                        r = rand();
                        if( r < 0.004 ) continue;
                        c = ( r < 0.06 ) ? b[int( rand()*4 )+1] : substr( anc, i, 1 );
                        out = out c;
                        if( rand() < 0.004 ) out = out b[int( rand()*4 )+1] b[int( rand()*4 )+1];
                    }
                    if( rc && s > 1 && s % rc == 0 ) {
                        rev = "";
                        for( i=length( out ); i>=1; i-- ) rev = rev comp[substr( out, i, 1 )];
                        out = rev;
                    }
                    printf( ">seq%03d\n%s\n", s, out );
                } }'
}
# Between them they reach all three --auto branches Dikarya hits.
family 1 120 650 0  > "$tmp/dna.fasta"      # the dikarya1 script's family
family 2 120 650 7  > "$tmp/dna_rc.fasta"   # 100-199 seqs < 1000 bp: --auto runs local pairs, 2 rounds
family 3 110 1200 9 > "$tmp/long_rc.fasta"  # >= 100 seqs, >= 1000 bp: --auto runs FFT-NS-i
family 4 24 500 5   > "$tmp/small_rc.fasta" # under 100 seqs: --auto runs full L-INS-i

check() {  # input, then MAFFT options
    local f=$1; shift
    env -u MAFFT_BINARIES nice -n 10 "$REFERENCE" --thread 1 --quiet "$@" "$f" >"$tmp/ref" 2>/dev/null || true
    env -u MAFFT_BINARIES nice -n 10 "$PREFIX/bin/mafft" --thread 1 --quiet "$@" "$f" >"$tmp/new" 2>/dev/null || true
    if [ -s "$tmp/ref" ] && cmp -s "$tmp/ref" "$tmp/new"; then
        echo "self-test ok:   $(basename "$f") $*"
    else
        echo "self-test FAIL: $(basename "$f") $*" >&2
        status=1
    fi
}
status=0
for f in "$tmp/dna.fasta" "$SRC/test/sample"; do
    check "$f" --auto
    check "$f" --localpair --maxiterate 1000
    check "$f" --retree 2 --maxiterate 2
done
for f in "$tmp/dna_rc.fasta" "$tmp/long_rc.fasta" "$tmp/small_rc.fasta"; do
    check "$f" --adjustdirection --localpair --maxiterate 1000
    check "$f" --adjustdirection --auto
done
check "$tmp/dna_rc.fasta" --adjustdirectionaccurately --retree 1 --maxiterate 0
check "$tmp/small_rc.fasta" --adjustdirectionaccurately --auto
check "$tmp/small_rc.fasta" --globalpair --maxiterate 1000
[ "$status" -eq 0 ] || { echo "do not switch MAFFT_BINARY to this build" >&2; exit 1; }

echo "next: set MAFFT_BINARY=$PREFIX/bin/mafft in /etc/dikarya/dikarya.environment.live"
