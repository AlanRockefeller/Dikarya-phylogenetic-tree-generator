# IQ-TREE build used by Dikarya

Dikarya runs IQ-TREE 3 for Quick Tree (`-m GTR+G -n 5 --alrt 1000`) and for the
advanced IQ-TREE method (`-m MFP -B 1000 --bnni --alrt 1000` and variants), always
through `_run_iqtree()` in `app/services/tree_builder_service.py`, with the binary
taken from `IQTREE_BINARY` (default `/usr/local/bin/iqtree3`).

| build | path | what it is |
|---|---|---|
| `3.1.4` | `/usr/local/bin/iqtree3` | the official release (GitHub `iqtree/iqtree3` v3.1.4, `Linux-intel` tarball) |
| `3.1.4-dikarya1` | `/opt/iqtree-3.1.4-dikarya1/bin/iqtree3` | v3.1.4 plus `iqtree-3.1.4-dikarya1.patch` (this directory) |

3.1.4 (2026-09-10) is the newest official release; `master` is at the same commit.

The patch series is published as a fork, https://github.com/AlanRockefeller/iqtree3
(branch `upstream-pr` = the code commits alone, for a pull request; `speedups` = the same
plus the documentation; tag `v3.1.4-dikarya1` = the source of the build here), with a full
write-up for the IQ-TREE maintainers in its `EXACT-SPEEDUPS.md` and a build-comparison
harness in `test_scripts/exact-speedups/`.

**dikarya1 gives the same results, faster.** For the same input, options and seed, at
`-T 1`, every output -- tree, UFBoot consensus and split supports, SH-aLRT values,
model parameters, distances, the `.iqtree` report and the log apart from timings and
the version line -- is byte-identical to the official 3.1.4 binary. `-T 2` is not
reproducible run to run in any build, stock included, because IQ-TREE schedules
parsimony trees dynamically and seeds each thread's SH-aLRT stream separately.

Since 2026-10-11 Dikarya runs IQ-TREE with `-T 1` on every worker (`IQTREE_THREADS` in
`app/services/tree_builder_service.py`), so every production tree is reproducible from
its recorded seed. Replaying 34 production jobs at both thread counts on dikarya1, a
second thread gained 1.02x on Quick Tree overall (0.89x-1.18x, gaining only past ~700
patterns) and made the three MFP+UFBoot+bnni jobs (58-80 taxa) 16-22% slower.

## Measured on this host (Xeon E5-2690 v4, 2 vCPUs, -T 1)

Real Dikarya alignments from `var/jobs` (inputs rebuilt exactly as `_run_iqtree`
does; replaying a job's own seed reproduces its stored tree byte for byte), each
command run with the official binary and dikarya1 interleaved:

| workload | official 3.1.4 | dikarya1 | speedup |
|---|---|---|---|
| Quick Tree, 48 inputs (8-969 taxa), total | 285.0 s | 191.9 s | 1.48x |
| Quick Tree, per job | median 3.94 s | median 2.49 s | median 1.63x (1.28x-2.29x) |
| `-m MFP -B 1000 --bnni --alrt 1000` (and K2P, `-B 5000`), 19 inputs, total | 4288 s | 2783 s | 1.54x |
| same, per job | | | median 1.75x (1.18x-2.66x; 9x on a 6-taxon job) |
| largest Quick Tree job (969 taxa, 627 patterns; in the 48) | 101.1 s | 79.0 s | 1.28x |
| 489 taxa, GTR+F+R6, `-n 3` (kernel-bound) | 257.9 s | 210.9 s | 1.22x |

The gain is largest where IQ-TREE did avoidable work (SH-aLRT replicates, UFBoot
bookkeeping) and smallest on very large trees, whose time is almost all in the
likelihood kernels, which cannot change their arithmetic without changing results.

## What the patch changes

The patch is a series of commits on v3.1.4; each commit message gives the exactness
argument and the measurement.

1. **SH-aLRT: draw the replicate pattern frequencies once per traversal.** Every branch
   re-seeded the same random streams and redrew the same 1000 bootstrap replicates
   (~20% of a Quick Tree run). They are drawn once and replayed. A debug build
   (`-DIQTREE_ALRT_CACHE_CHECK`) redraws and compares every replayed replicate; it was
   used to check `-T 2`.
2. **UFBoot: print a candidate tree only if some replicate takes it** (~27% of a
   `-B 1000 --bnni` run went to printing trees nobody stored).
3. **PLL parsimony with `-mpopcnt`** (integer-only starting-tree code).
4. **UFBoot: refresh the checkpoint only when it is about to be dumped.** All 1000 boot
   trees were re-serialized after every refined tree; the file is written once a minute.
5. **UFBoot: sorted topology strings without per-subtree streams**; branch lengths via
   `std::to_chars`, which is specified to match the stream's `printf` formatting
   (checked on 10.6M values).
6. **Node accessors inline**, and **no OpenMP runtime call when there is one thread**.
7. **Kernels: tip partial likelihoods interleaved by 4x4 vector transposes** instead of
   scalar stores that the CPU could not forward to the following vector loads, and
   **two pattern blocks per pass** in the internal-node kernel, sharing coefficient loads.
8. **Version `3.1.4-dikarya1`**, so a job's `logs/tree_builder.log` names the binary.

Tried and not adopted: PGO (3% on Quick Tree, nothing on UFBoot), `-march=native`
(Eigen and vectorclass change packet width and FMA use under `__AVX2__`/`__FMA__`,
which changes results), LTO (could fuse a non-FMA `a*b+c` into an FMA function).

## Verification before adoption

- Stock 3.1.4 rebuilt with the release's toolchain (clang 22, static glibc, Ubuntu
  22.04 Eigen/Boost headers, bundled libomp/zlib) reproduces the official binary
  byte for byte; dikarya1 is that build plus the patch.
- dikarya1 vs the official binary at `-T 1`: 67 real inputs (48 Quick Tree, 19
  MFP/K2P UFBoot+bnni runs), all identical; 36 runs covering every option shape
  `_run_iqtree` can produce (Quick Tree; MFP/K2P/HKY+G/GTR+F+R4; `-B 1000`/`-B 5000`,
  with and without `--bnni`; with and without `--alrt`; `--fast`), all identical.
- `-DIQTREE_ALRT_CACHE_CHECK` build: 413,000 replayed SH-aLRT replicates on five
  alignments at `-T 1` and `-T 2`, both resampling paths, all equal to a fresh draw.

## Installing / switching / rolling back

As root on the production host:

```zsh
hostname                                   # must print mycolab
bash /var/www/dikarya/ops/iqtree/install-iqtree-dikarya.sh
```

The script unpacks the pinned toolchain (from `cache/iqtree-toolchain/` in the
repository, or downloaded and checked against the SHA-256 sums in the script), clones
v3.1.4, applies the patch, builds (about 15-25 minutes on 2 vCPUs, at `nice 10`), and
refuses to install unless the new binary reproduces `/usr/local/bin/iqtree3` byte for
byte on a synthetic alignment for the Quick Tree, MFP+UFBoot+bnni and plain UFBoot
commands. Then point Dikarya at it -- comment out any existing `IQTREE_BINARY` line
rather than adding a second one -- and have an agent restart the workers with the
guarded wrapper (see "Restarting Dikarya services" in `CLAUDE.md`):

```zsh
grep -n IQTREE_BINARY /etc/dikarya/dikarya.environment.live
sed -i 's|^IQTREE_BINARY=.*$|# & (replaced by dikarya1)|' /etc/dikarya/dikarya.environment.live
echo 'IQTREE_BINARY=/opt/iqtree-3.1.4-dikarya1/bin/iqtree3' >> /etc/dikarya/dikarya.environment.live
grep -n IQTREE_BINARY /etc/dikarya/dikarya.environment.live
```

New jobs' `logs/tree_builder.log` then start with `IQ-TREE version 3.1.4-dikarya1`.
To roll back, comment out the new line (and restore the old one, if there was one) and
restart the workers; `/usr/local/bin/iqtree3` is untouched.

Both binaries, official and dikarya1, link LLVM's OpenMP runtime statically; with an
LLVM toolchain's `lib/` on `LD_LIBRARY_PATH` that runtime loads the toolchain's
OpenMP tool plugins at startup and spins forever without output. The install script
keeps the toolchain's libraries away from every IQ-TREE run; do the same by hand.
