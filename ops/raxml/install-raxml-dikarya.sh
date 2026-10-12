#!/bin/bash
# Build and install RAxML-NG 2.0.3-dikarya1 next to the installed raxml-ng.
#
#   sudo bash ops/raxml/install-raxml-dikarya.sh
#
# Clones RAxML-NG 2.0.3 (with its coraxlib submodule) at the release commit,
# builds it twice with the release's configuration -- once as is, once with the
# patch series in this directory (raxml-ng-2.0.3-dikarya1.patch for RAxML-NG,
# coraxlib-2.0.3-dikarya1.patch for coraxlib) -- and installs the patched build
# into $PREFIX. Nothing in Dikarya changes until RAXML_BINARY in
# /etc/dikarya/dikarya.environment.live points at the new prefix (see
# ops/raxml/README.md). Re-running it rebuilds from scratch.
#
# The script refuses to finish unless the patched binary reproduces the stock
# 2.0.3 build byte for byte on a synthetic alignment, for the command shapes
# Dikarya runs. (The installed 2.0.2 is not the reference: 2.0.3 itself changes
# results, e.g. it rounds branch lengths in parsimony starting trees.)
set -euo pipefail

PREFIX=${PREFIX:-/opt/raxml-ng-2.0.3-dikarya1}
SRC=${SRC:-/usr/local/src/raxml-ng-2.0.3-dikarya1}
UPSTREAM=https://github.com/amkozlov/raxml-ng.git
TAG=2.0.3
BASE=173b012f9989bfdd53970e6917e4037be3d1e38e
CORAX_BASE=a2c8c6984438ed1e7aadd354f8396a1ddbdc3b69
HERE=$(cd "$(dirname "$0")" && pwd)
PATCH=$HERE/raxml-ng-2.0.3-dikarya1.patch
CORAX_PATCH=$HERE/coraxlib-2.0.3-dikarya1.patch
GCC_WANT="13.3.0"   # Ubuntu 24.04's gcc, the compiler of the official release binaries

if [ "$(id -u)" -ne 0 ]; then echo "run as root" >&2; exit 77; fi
for f in "$PATCH" "$CORAX_PATCH" "$HERE/selftest-data.awk" "$HERE/compare-raxml-runs.py"; do
    [ -f "$f" ] || { echo "missing $f" >&2; exit 1; }
done
for tool in git cmake make g++ gcc python3 awk; do
    command -v "$tool" >/dev/null || { echo "missing build tool: $tool" >&2; exit 1; }
done
gcc_v=$(gcc -dumpfullversion)
[ "$gcc_v" = "$GCC_WANT" ] \
    || echo "note: gcc $gcc_v, not $GCC_WANT; the self-test still compares like with like" >&2

rm -rf "$SRC"
mkdir -p "$SRC"

# ---- source ----------------------------------------------------------------
git clone --quiet --branch "$TAG" --recurse-submodules "$UPSTREAM" "$SRC/stock"
[ "$(git -C "$SRC/stock" rev-parse HEAD)" = "$BASE" ] \
    || { echo "$TAG is not $BASE any more; refusing to build" >&2; exit 1; }
[ "$(git -C "$SRC/stock/libs/coraxlib" rev-parse HEAD)" = "$CORAX_BASE" ] \
    || { echo "coraxlib submodule is not $CORAX_BASE; refusing to build" >&2; exit 1; }
cp -a "$SRC/stock" "$SRC/dikarya1"
am() { git -C "$1" -c user.name=install-raxml-dikarya -c user.email=root@localhost \
           am --quiet --committer-date-is-author-date "$2"; }
am "$SRC/dikarya1/libs/coraxlib" "$CORAX_PATCH"
am "$SRC/dikarya1" "$PATCH"

# ---- build -----------------------------------------------------------------
# The release's configuration (ci/Dockerfile: Release = -O3, static, portable
# with run-time SIMD dispatch) without terraphast and VCF input, which need
# libgmp/htslib and which Dikarya never uses.
build() {
    mkdir -p "$SRC/build-$1"
    (cd "$SRC/build-$1" &&
     cmake "$SRC/$1" -DCMAKE_BUILD_TYPE=Release -DSTATIC_BUILD=ON -DPORTABLE_BUILD=ON \
         -DUSE_TERRAPHAST=OFF -DUSE_VCF=OFF >"$SRC/cmake-$1.log" 2>&1 \
         || { tail -30 "$SRC/cmake-$1.log"; exit 1; }
     nice -n 10 make -j"$(nproc)" >"$SRC/build-$1.log" 2>&1 \
         || { grep -m20 "error" "$SRC/build-$1.log"; tail -5 "$SRC/build-$1.log"; exit 1; })
}
build stock
build dikarya1
REF=$SRC/build-stock/bin/raxml-ng
NEW=$SRC/build-dikarya1/bin/raxml-ng
"$NEW" --version | sed -n 2p

# ---- self-test: identical results to stock 2.0.3 ---------------------------
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
awk -f "$HERE/selftest-data.awk" > "$tmp/aln.fasta"
status=0
n=0
# the shapes _run_raxml() and _run_moose() produce: tree + autoMRE bootstrap
# with both support metrics, the KH stopping rule, FreeRate/+I models, ML search
# only, a fixed bootstrap count, and MOOSE model selection
while IFS= read -r opts; do
    n=$((n+1))
    for side in ref new; do
        bin=$REF; [ "$side" = new ] && bin=$NEW
        mkdir -p "$tmp/$side$n"
        # shellcheck disable=SC2086
        nice -n 10 "$bin" --msa "$tmp/aln.fasta" $opts --threads 1 --seed 20261009 --redo \
            --prefix "$tmp/$side$n/run" >/dev/null 2>&1 || { echo "$side build failed: $opts" >&2; status=1; }
    done
    if python3 -I "$HERE/compare-raxml-runs.py" "$tmp/ref$n/run" "$tmp/new$n/run"; then
        echo "self-test ok:   $opts"
    else
        echo "self-test FAIL: $opts" >&2
        status=1
    fi
done <<'EOF'
--all --model GTR+G --tree pars{3},rand{2} --bs-trees autoMRE{100} --bs-metric fbp,tbe
--all --model HKY+I+G --tree pars{2} --bs-trees autoMRE{60} --bs-metric fbp,tbe --stop-rule kh-mult
--all --model TIM2+I+R3 --tree pars{2} --bs-trees 10 --bs-metric fbp,tbe
--search --model TN93+R2 --tree pars{3},rand{1}
--moose --data-type DNA --moose-options criterion=bic/rhas=G,R,I+G,I+R/freerate-categories=2-5
EOF
[ "$status" -eq 0 ] || { echo "not installed: the patched build does not reproduce stock $TAG" >&2; exit 1; }

install -D -m 0755 "$NEW" "$PREFIX/bin/raxml-ng"
install -D -m 0644 "$PATCH" "$PREFIX/share/raxml-ng-2.0.3-dikarya1.patch"
install -D -m 0644 "$CORAX_PATCH" "$PREFIX/share/coraxlib-2.0.3-dikarya1.patch"
echo "installed: $("$PREFIX/bin/raxml-ng" --version | sed -n 2p)"
echo "next: set RAXML_BINARY=$PREFIX/bin/raxml-ng in /etc/dikarya/dikarya.environment.live"
