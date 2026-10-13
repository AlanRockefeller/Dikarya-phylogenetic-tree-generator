#!/bin/bash
# Build and install IQ-TREE 3.1.4-dikarya1 next to the official 3.1.4 binary.
#
#   sudo bash ops/iqtree/install-iqtree-dikarya.sh
#
# Clones IQ-TREE 3.1.4 at the release commit, applies the patch series in
# ops/iqtree/iqtree-3.1.4-dikarya1.patch, builds it with the toolchain the
# official release was built with (clang 22, Ubuntu 22.04's Eigen 3.4.0 and
# Boost 1.74 headers, IQ-TREE's bundled libomp/zlib, a static binary) and
# installs into $PREFIX. Nothing in Dikarya changes until IQTREE_BINARY in
# /etc/dikarya/dikarya.environment.live points at the new prefix (see
# ops/iqtree/README.md). Re-running it rebuilds from scratch.
#
# The toolchain is unpacked from pinned .deb files into the build directory; it
# is not installed system-wide. They are read from $CACHE when present
# (cache/iqtree-toolchain in the repository) and downloaded otherwise; either way
# each one must match the SHA-256 below.
#
# The script refuses to finish unless the new binary reproduces the installed
# official binary ($REFERENCE) byte for byte at -T 1 on a synthetic alignment,
# for the option shapes Dikarya runs.
set -euo pipefail
# (pipes below read all their input -- sed -n 1p, not head -1 -- so pipefail never
# sees a SIGPIPE from a version banner)

PREFIX=${PREFIX:-/opt/iqtree-3.1.4-dikarya1}
SRC=${SRC:-/usr/local/src/iqtree-3.1.4-dikarya1}
REFERENCE=${REFERENCE:-/usr/local/bin/iqtree3}
UPSTREAM=https://github.com/iqtree/iqtree3.git
TAG=v3.1.4
BASE=63c330d90dd02241dbbbaf1e9f9e9cc6dadbd1de
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
CACHE=${CACHE:-$REPO/cache/iqtree-toolchain}
PATCH=$HERE/iqtree-3.1.4-dikarya1.patch

LLVM_POOL=https://apt.llvm.org/noble/pool/main/l/llvm-toolchain-22
LLVM_V='22.1.8~++20260714014902+ca7933e47d3a-1~exp1~20260714135019.80'
DEBS=$(cat <<EOF
e969e7f97a6e71326bae2cec6d76059cb9bf6562bfe89d03bcb65c3cee1aac05 clang-22_${LLVM_V}_amd64.deb
90cee393eefbe1cb5bf463b7d4aea4ab5512d06525641e6836d21c4d7191944e libclang-common-22-dev_${LLVM_V}_amd64.deb
620b7960fef6ca4647b813889d4f3ae3f8bd5de71c949550c98a6030427acb18 libclang-cpp22_${LLVM_V}_amd64.deb
5ec77bbfbdae76a6eda76ef9fff0cfd983d6b9c7ea72335a7fed9c99c423a7fb libclang-rt-22-dev_${LLVM_V}_amd64.deb
e94d23161316c53ebe8c9e35038468e06a5e47dbdf207a3c54a22ab40e37cbb6 libllvm22_${LLVM_V}_amd64.deb
de413d522ae9893c595def5df40d3901ce4df10029f3cb6306046bfd89a10449 libomp-22-dev_${LLVM_V}_amd64.deb
4172d3b289825f729a84ae351b2f37a2c6c6715f2d1511edd60b59a79c3da200 lld-22_${LLVM_V}_amd64.deb
402a671415ac47810b4e581c06fa6e29110e51efae697647f7072e0528a1718e llvm-22_${LLVM_V}_amd64.deb
ee6df5de094e62ba016f6d8ade319d34c246a0a5c79ed898aeaaa327332cd5b7 llvm-22-linker-tools_${LLVM_V}_amd64.deb
04ee3759712a0f003fb186edf83724947826d7a43f3ef8d858cd359ca38a25ef libeigen3-dev_3.4.0-2ubuntu2_all.deb
4d9c90e43f0d25db6280d1ee326771cbb76462f73b9430f06bac1de8d05b7a78 libboost1.74-dev_1.74.0-14ubuntu3_amd64.deb
EOF
)

if [ "$(id -u)" -ne 0 ]; then echo "run as root" >&2; exit 77; fi
for f in avx2 fma popcnt; do
    grep -qw "$f" /proc/cpuinfo || { echo "this CPU lacks $f; the build is tuned for an AVX2+FMA host" >&2; exit 1; }
done
[ -f "$PATCH" ] || { echo "missing $PATCH" >&2; exit 1; }
[ -x "$REFERENCE" ] || { echo "no reference binary at $REFERENCE (set REFERENCE=...)" >&2; exit 1; }
"$REFERENCE" --version 2>/dev/null | grep "IQ-TREE version 3.1.4 " >/dev/null \
    || { echo "$REFERENCE is not IQ-TREE 3.1.4; the identity self-test needs the official 3.1.4" >&2; exit 1; }

rm -rf "$SRC"
mkdir -p "$SRC/debs" "$SRC/toolchain"

# ---- toolchain -------------------------------------------------------------
echo "$DEBS" | while read -r sum name; do
    if [ -f "$CACHE/$name" ]; then
        cp "$CACHE/$name" "$SRC/debs/"
    else
        case "$name" in
            libeigen3-dev*|libboost1.74-dev*)
                url="https://launchpad.net/ubuntu/+archive/primary/+files/$name" ;;
            *)  url="$LLVM_POOL/$(printf '%s' "$name" | sed 's/+/%2B/g')" ;;
        esac
        curl -fsSL -o "$SRC/debs/$name" "$url" || { echo "cannot download $name" >&2; exit 1; }
    fi
    echo "$sum  $SRC/debs/$name" | sha256sum -c --quiet - \
        || { echo "checksum mismatch: $name" >&2; exit 1; }
    dpkg-deb -x "$SRC/debs/$name" "$SRC/toolchain"
done
LLVM=$SRC/toolchain/usr/lib/llvm-22
# Run only the build with the toolchain's libraries on LD_LIBRARY_PATH (and its
# bin/ on PATH: IQ-TREE's CMake requires ld.lld for clang). Never run IQ-TREE
# that way: its statically linked OpenMP runtime looks for tool plugins (libompd,
# libarcher) at startup, finds this toolchain's newer ones and spins forever --
# the official binary does the same.
tc() { LD_LIBRARY_PATH="$LLVM/lib:$SRC/toolchain/usr/lib/x86_64-linux-gnu" PATH="$LLVM/bin:$PATH" "$@"; }
tc "$LLVM/bin/clang++" --version | sed -n 1p

# ---- source ----------------------------------------------------------------
git clone --quiet --branch "$TAG" --recurse-submodules "$UPSTREAM" "$SRC/iqtree3"
[ "$(git -C "$SRC/iqtree3" rev-parse HEAD)" = "$BASE" ] \
    || { echo "$TAG is not $BASE any more; refusing to build" >&2; exit 1; }
git -C "$SRC/iqtree3" -c user.name=install-iqtree-dikarya -c user.email=root@localhost \
    am --keep-cr --quiet --committer-date-is-author-date "$PATCH"

# ---- build -----------------------------------------------------------------
# The release's configuration (IQTREE_FLAGS=static; CMake picks -O3 for clang),
# except USE_MUTSEL=OFF: the Rust mutation-selection models, which need a Rust
# toolchain and which Dikarya never selects.
mkdir -p "$SRC/build"
cd "$SRC/build"
tc cmake "$SRC/iqtree3" -DCMAKE_POLICY_VERSION_MINIMUM=3.5 -DIQTREE_FLAGS=static -DUSE_MUTSEL=OFF \
    -DCMAKE_C_COMPILER="$LLVM/bin/clang" -DCMAKE_CXX_COMPILER="$LLVM/bin/clang++" \
    -DEIGEN3_INCLUDE_DIR="$SRC/toolchain/usr/include/eigen3" \
    -DBoost_INCLUDE_DIR="$SRC/toolchain/usr/include" \
    -DCMAKE_EXE_LINKER_FLAGS="-fuse-ld=lld" >"$SRC/cmake.log" 2>&1 \
    || { tail -30 "$SRC/cmake.log"; exit 1; }
tc nice -n 10 make -j"$(nproc)" iqtree3 >"$SRC/build.log" 2>&1 || { grep -m20 "error:" "$SRC/build.log"; tail -5 "$SRC/build.log"; exit 1; }
"$SRC/build/iqtree3" --version | sed -n 1p

# ---- self-test: identical results to the official binary ---------------------
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
awk -f "$HERE/selftest-data.awk" > "$tmp/aln.fasta"
status=0
n=0
for opts in "-m GTR+G -n 5 --alrt 1000" \
            "-m MFP -B 1000 --bnni --alrt 1000" \
            "-m HKY+G -B 1000 --alrt 1000"; do
    n=$((n+1))
    for side in ref new; do
        bin=$REFERENCE; [ "$side" = new ] && bin=$SRC/build/iqtree3
        mkdir -p "$tmp/$side$n"
        # shellcheck disable=SC2086
        nice -n 10 "$bin" -s "$tmp/aln.fasta" $opts -T 1 --seed 20261007 --redo \
            --prefix "$tmp/$side$n/run" >/dev/null 2>&1 || { echo "$side build failed: $opts" >&2; status=1; }
    done
    if python3 -I "$HERE/compare-iqtree-runs.py" "$tmp/ref$n/run" "$tmp/new$n/run"; then
        echo "self-test ok:   $opts"
    else
        echo "self-test FAIL: $opts" >&2
        status=1
    fi
done
[ "$status" -eq 0 ] || { echo "not installed: the new build does not reproduce $REFERENCE" >&2; exit 1; }

install -D -m 0755 "$SRC/build/iqtree3" "$PREFIX/bin/iqtree3"
install -D -m 0644 "$PATCH" "$PREFIX/share/iqtree-3.1.4-dikarya1.patch"
echo "installed: $("$PREFIX/bin/iqtree3" --version | sed -n 1p)"
echo "next: set IQTREE_BINARY=$PREFIX/bin/iqtree3 in /etc/dikarya/dikarya.environment.live"
