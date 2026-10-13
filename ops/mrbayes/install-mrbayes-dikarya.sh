#!/bin/bash
# Build and install MrBayes 3.2.9-dikarya2 (with its own BEAGLE 4.0.1-dikarya2)
# next to Ubuntu's mb.
#
#   sudo bash ops/mrbayes/install-mrbayes-dikarya.sh
#
# Clones MrBayes at the 3.2.9 release-branch commit and BEAGLE at the 4.0.1
# tag, and builds each twice -- stock, and with the patch series in this
# directory -- in the configuration of Ubuntu's mrbayes and libhmsbeagle
# packages: MrBayes with Debian's compiler flags and --enable-debug (so its
# run-time checks, including the "Log likelihood nan" abort Dikarya relies on,
# stay in), BEAGLE without -march=native (the first BEAGLE patch; the stock
# build gets it too, as Debian's does). The patched pair is installed into
# $PREFIX with mb linked to the patched library there. Nothing in Dikarya
# changes until MRBAYES_BINARY in /etc/dikarya/dikarya.environment.live points
# at it (see ops/mrbayes/README.md). Re-running it rebuilds from scratch.
#
# The script refuses to install unless the patched build reproduces the stock
# 3.2.9 build byte for byte on a synthetic alignment, for the analyses Dikarya
# runs, at one and two threads (at two, the chains' likelihoods are computed
# concurrently). (Ubuntu's mb 3.2.7a is not the reference: 3.2.8/3.2.9 change
# results by themselves, e.g. 3.2.9 corrects the NodeSlider proposal ratio.)
set -euo pipefail

VER=dikarya2
PREFIX=${PREFIX:-/opt/mrbayes-3.2.9-$VER}
SRC=${SRC:-/usr/local/src/mrbayes-3.2.9-$VER}
MB_UPSTREAM=https://github.com/NBISweden/MrBayes.git
MB_BRANCH=release/v3.2.9
MB_BASE=4f741bcfe41f65b7f4d560cec98614d3d9e29326
BG_UPSTREAM=https://github.com/beagle-dev/beagle-lib.git
BG_TAG=v4.0.1
BG_BASE=bf35ad4a28e5f03edaa10dfd77c1b54eee0d9595
HERE=$(cd "$(dirname "$0")" && pwd)
MB_PATCH=$HERE/mrbayes-3.2.9-$VER.patch
BG_PATCH=$HERE/beagle-4.0.1-$VER.patch

# Debian/Ubuntu 24.04 package build flags (dpkg-buildflags with hardening=+all)
DEB_CFLAGS="-g -O2 -fno-omit-frame-pointer -mno-omit-leaf-frame-pointer -flto=auto -ffat-lto-objects -fstack-protector-strong -fstack-clash-protection -Wformat -Werror=format-security -fcf-protection"
DEB_CPPFLAGS="-Wdate-time -D_FORTIFY_SOURCE=3"
DEB_LDFLAGS="-Wl,-Bsymbolic-functions -flto=auto -ffat-lto-objects -Wl,-z,relro -Wl,-z,now"

if [ "$(id -u)" -ne 0 ]; then echo "run as root" >&2; exit 77; fi
for f in "$MB_PATCH" "$BG_PATCH" "$HERE/selftest-data.awk" "$HERE/compare-mrbayes-runs.py"; do
    [ -f "$f" ] || { echo "missing $f" >&2; exit 1; }
done
for tool in git cmake make g++ gcc python3 awk; do
    command -v "$tool" >/dev/null || { echo "missing build tool: $tool" >&2; exit 1; }
done
grep -qw avx2 /proc/cpuinfo || echo "note: no AVX2 here; the patched kernels fall back to the original code" >&2

rm -rf "$SRC"
mkdir -p "$SRC"
am() { git -C "$1" -c user.name=install-mrbayes-dikarya -c user.email=root@localhost \
           am --quiet --committer-date-is-author-date "$2"; }

# ---- source ----------------------------------------------------------------
git clone --quiet --branch "$MB_BRANCH" "$MB_UPSTREAM" "$SRC/mb-stock"
git -C "$SRC/mb-stock" -c advice.detachedHead=false checkout --quiet "$MB_BASE" \
    || { echo "$MB_BASE is not on $MB_BRANCH any more; refusing to build" >&2; exit 1; }
git -c advice.detachedHead=false clone --quiet --branch "$BG_TAG" "$BG_UPSTREAM" "$SRC/bg-stock"
[ "$(git -C "$SRC/bg-stock" rev-parse HEAD)" = "$BG_BASE" ] \
    || { echo "$BG_TAG is not $BG_BASE any more; refusing to build" >&2; exit 1; }
cp -a "$SRC/mb-stock" "$SRC/mb-$VER"
cp -a "$SRC/bg-stock" "$SRC/bg-$VER"
am "$SRC/mb-$VER" "$MB_PATCH"
am "$SRC/bg-$VER" "$BG_PATCH"
# the stock reference is built like the distribution's: patch 1 only
# (no -march=native, which lets g++ fuse multiplies and adds into FMAs)
awk '/^From [0-9a-f]+ /{n++} n==1' "$BG_PATCH" > "$SRC/beagle-build.patch"
am "$SRC/bg-stock" "$SRC/beagle-build.patch"

# ---- build -----------------------------------------------------------------
# Everything is built and tested under $SRC; $PREFIX is only written by the
# final install step, so re-running this while Dikarya uses $PREFIX is safe.
# BEAGLE's core library loads its CPU plugin by name: a run path of $ORIGIN
# makes it find the one beside it. mb's run path is the final $PREFIX/lib;
# the self-test points LD_LIBRARY_PATH at the build's libraries instead.
build_beagle() {   # <name>
    mkdir -p "$SRC/build-bg-$1"
    (cd "$SRC/build-bg-$1" &&
     cmake "$SRC/bg-$1" -DBUILD_OPENCL=OFF -DBUILD_CUDA=OFF -DBUILD_JNI=OFF -DBUILD_SSE=OFF \
         -DCMAKE_INSTALL_PREFIX="$SRC/bg-$1-inst" -DCMAKE_INSTALL_RPATH='$ORIGIN' \
         >"$SRC/cmake-bg-$1.log" 2>&1 || { tail -30 "$SRC/cmake-bg-$1.log"; exit 1; }
     nice -n 10 make -j"$(nproc)" hmsbeagle hmsbeagle-cpu >"$SRC/build-bg-$1.log" 2>&1 \
         || { grep -m20 "error" "$SRC/build-bg-$1.log"; exit 1; }
     make install >"$SRC/install-bg-$1.log" 2>&1 || { tail -20 "$SRC/install-bg-$1.log"; exit 1; })
}
build_mrbayes() {  # <name> <run path>
    mkdir -p "$SRC/build-mb-$1"
    (cd "$SRC/build-mb-$1" &&
     CFLAGS="$DEB_CFLAGS" CPPFLAGS="$DEB_CPPFLAGS" LDFLAGS="$DEB_LDFLAGS" \
     BEAGLE_CFLAGS="-I$SRC/bg-$1-inst/include/libhmsbeagle-1" \
     BEAGLE_LIBS="-L$SRC/bg-$1-inst/lib -Wl,--enable-new-dtags -Wl,-rpath,$2 -lhmsbeagle" \
         "$SRC/mb-$1/configure" --with-beagle=yes --enable-debug=yes \
             --disable-sse --disable-avx --disable-fma --without-readline \
             >"$SRC/configure-mb-$1.log" 2>&1 || { tail -30 "$SRC/configure-mb-$1.log"; exit 1; }
     nice -n 10 make -j"$(nproc)" -C src >"$SRC/build-mb-$1.log" 2>&1 \
         || { grep -m20 "error" "$SRC/build-mb-$1.log"; exit 1; })
}
build_beagle stock
build_beagle "$VER"
build_mrbayes stock "$SRC/bg-stock-inst/lib"
build_mrbayes "$VER" "$PREFIX/lib"
REF=$SRC/build-mb-stock/src/mb
NEW=$SRC/build-mb-$VER/src/mb
REF_LIB=$SRC/bg-stock-inst/lib
NEW_LIB=$SRC/bg-$VER-inst/lib
LD_LIBRARY_PATH=$NEW_LIB "$NEW" -v | grep -i version

# ---- self-test: identical results to stock 3.2.9 ---------------------------
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
awk -f "$HERE/selftest-data.awk" > "$tmp/data.nex"
status=0
n=0
# The blocks _run_mrbayes() writes: GTR+G with the split-frequency stop rule
# (the default), the other substitution/rate models it maps to, one run of
# one chain, 3x3 chains; and the same with the thread budget, which computes
# the chains' likelihoods concurrently -- except for the single chain, which
# BEAGLE splits by pattern instead (forced on for this 480-pattern alignment,
# which would otherwise not be split).
while IFS='|' read -r env pre lset prset mcmc; do
    n=$((n+1))
    for side in ref new; do
        bin=$REF; lib=$REF_LIB; [ "$side" = new ] && { bin=$NEW; lib=$NEW_LIB; }
        d=$tmp/$side$n; mkdir -p "$d"
        { cat "$tmp/data.nex"
          printf '\nbegin mrbayes;\n   set autoclose=yes nowarn=yes;\n'
          [ -n "$pre" ] && printf '   %s\n' "$pre"
          printf '   set seed=20261009 swapseed=20261009;\n   lset %s;\n' "$lset"
          [ -n "$prset" ] && printf '   prset %s;\n' "$prset"
          printf '   mcmc %s relburnin=yes burninfrac=0.25;\n' "$mcmc"
          printf '   sump relburnin=yes burninfrac=0.25;\n   sumt relburnin=yes burninfrac=0.25;\n   quit;\nend;\n'
        } > "$d/input.nex"
        # shellcheck disable=SC2086
        (cd "$d" && env $env LD_LIBRARY_PATH="$lib" nice -n 10 "$bin" input.nex > stdout.txt 2>&1) \
            || { echo "$side build failed: $lset $mcmc" >&2; status=1; }
    done
    if python3 -I "$HERE/compare-mrbayes-runs.py" "$tmp/ref$n" "$tmp/new$n" -q; then
        echo "self-test ok:   lset $lset; mcmc $mcmc ${pre:+($pre)}"
    else
        echo "self-test FAIL: lset $lset; mcmc $mcmc ${pre:+($pre)}" >&2
        status=1
    fi
done <<'EOF'
X=1||nst=6 rates=gamma||ngen=3000 nchains=4 nruns=2 samplefreq=20 printfreq=500 mcmcdiagn=yes stoprule=yes stopval=0.01
X=1||nst=2 rates=invgamma||ngen=2000 nchains=4 nruns=2 samplefreq=20 printfreq=500 mcmcdiagn=yes stoprule=yes stopval=0.01
X=1||nst=1 rates=equal|statefreqpr=fixed(equal)|ngen=2000 nchains=4 nruns=2 samplefreq=20 printfreq=500
X=1||nst=6 rates=propinv|statefreqpr=fixed(equal)|ngen=2000 nchains=4 nruns=2 samplefreq=20 printfreq=500
X=1||nst=1 rates=gamma||ngen=3000 nchains=1 nruns=1 samplefreq=20 printfreq=500
X=1||nst=6 rates=invgamma||ngen=2000 nchains=3 nruns=3 samplefreq=20 printfreq=500
X=1|set beaglethreadcount=2;|nst=6 rates=gamma||ngen=3000 nchains=4 nruns=2 samplefreq=20 printfreq=500 mcmcdiagn=yes stoprule=yes stopval=0.01
X=1|set beaglethreadcount=2;|nst=2 rates=invgamma||ngen=2000 nchains=4 nruns=2 samplefreq=20 printfreq=500 mcmcdiagn=yes stoprule=yes stopval=0.01
X=1|set beaglethreadcount=2;|nst=6 rates=invgamma||ngen=2000 nchains=3 nruns=3 samplefreq=20 printfreq=500
X=1|set beaglethreadcount=2;|nst=1 rates=equal|statefreqpr=fixed(equal)|ngen=2000 nchains=2 nruns=2 samplefreq=20 printfreq=500
BEAGLE_DIKARYA_MIN_PATTERNS=1|set beaglethreadcount=2;|nst=1 rates=gamma||ngen=3000 nchains=1 nruns=1 samplefreq=20 printfreq=500
EOF
[ "$status" -eq 0 ] || { echo "not installed: the patched build does not reproduce stock 3.2.9" >&2; exit 1; }

# ---- install (new files replace old ones by rename: running jobs are fine) --
mkdir -p "$PREFIX/bin" "$PREFIX/lib" "$PREFIX/share"
for f in "$NEW_LIB"/libhmsbeagle*.so*; do
    if [ -L "$f" ]; then
        ln -sfn "$(readlink "$f")" "$PREFIX/lib/$(basename "$f")"
    else
        install -m 0644 "$f" "$PREFIX/lib/$(basename "$f")"
    fi
done
install -m 0755 "$NEW" "$PREFIX/bin/mb"
install -m 0644 "$MB_PATCH" "$PREFIX/share/$(basename "$MB_PATCH")"
install -m 0644 "$BG_PATCH" "$PREFIX/share/$(basename "$BG_PATCH")"

# the installed binary, with no library path set, must load the patched
# library from $PREFIX and still reproduce the first self-test case
d=$tmp/installed; mkdir -p "$d"; cp "$tmp/new1/input.nex" "$d/"
(cd "$d" && env -u LD_LIBRARY_PATH nice -n 10 "$PREFIX/bin/mb" input.nex > stdout.txt 2>&1) || true
grep -q "Using BEAGLE v4.0.1-$VER" "$d/stdout.txt" \
    || { echo "installed mb does not load $PREFIX/lib's BEAGLE" >&2; exit 1; }
python3 -I "$HERE/compare-mrbayes-runs.py" "$tmp/ref1" "$d" -q \
    || { echo "installed mb does not reproduce the self-test" >&2; exit 1; }
echo "installed: $PREFIX/bin/mb ($("$PREFIX/bin/mb" -v | grep -i version | tr -s ' '))"
echo "next: set MRBAYES_BINARY=$PREFIX/bin/mb in /etc/dikarya/dikarya.environment.live"
