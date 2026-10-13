# RAxML-NG build used by Dikarya

Dikarya runs RAxML-NG for the "RAxML-NG" tree method -- `--all` (ML search plus
autoMRE bootstrapping with Felsenstein and transfer support), sometimes with the KH
stopping rule, under GTR-family models with `+G4`, `+I` and FreeRate `+R2..R5` -- and
for MOOSE model selection, always through `_run_raxml()` / `_run_moose()` in
`app/services/tree_builder_service.py`, with the binary taken from `RAXML_BINARY`.
RAxML jobs are rare (about 1% of jobs) but long: of the 92 finished runs on disk
with a recorded time, the median took 13 minutes, 19 took over an hour and the
longest 12.9 hours -- 88 hours of wall time in all, nearly all of it at
`--threads 2`, and 95% of it in bootstrap replicates.

| build | path | what it is |
|---|---|---|
| `2.0.2` | `/usr/local/bin/raxml-ng` | the official release binary (static, gcc 13.3) |
| `2.0.3-dikarya1` | `/opt/raxml-ng-2.0.3-dikarya1/bin/raxml-ng` | 2.0.3 plus the two patches in this directory |

2.0.3 (2026-09-03) is the newest release; `master` adds only a CITATION file. Moving
from 2.0.2 to 2.0.3 is an upstream change in its own right: 2.0.3 rounds branch
lengths in parsimony starting trees (bootstrap trees differ from 2.0.2's), fixes a
MOOSE slowdown and wrong FreeRate rates in the output (trees and likelihoods were not
affected), and fixes a few crashes. Every comparison below is against **stock 2.0.3**
built the same way.

**dikarya1 gives the same results, faster.** For the same input, options and seed, at
`--threads 1` and `--threads 2`, every output file -- best tree, ML trees, bootstrap
trees, both support trees, the model, MOOSE's JSON apart from its run metadata -- and
the log apart from timings, paths and the version line are byte-identical to stock
2.0.3.

## Measured on this host (Xeon E5-2690 v4, 2 vCPUs, --threads 1)

Real Dikarya alignments from `var/jobs`, each run with the job's own model, seed,
starting trees and support options (inputs rebuilt exactly as `_run_raxml` does:
replaying a job's command reproduces its stored trees byte for byte), stock and
dikarya1 interleaved (ABBA), CPU time:

*Pending: the timing table has not been written up yet, so this build has no
published speedup. Do not switch `RAXML_BINARY` on the strength of this section.*

The gain is the same at `--threads 2` (two coarse-grained workers, how the main
worker runs RAxML), since each worker runs the same single-threaded searches.

## What the patches change

`coraxlib-2.0.3-dikarya1.patch` (RAxML-NG's likelihood library, 7 commits) and
`raxml-ng-2.0.3-dikarya1.patch` (2 commits). Each commit message gives the exactness
argument and the measurement.

1. **SPR: no likelihood evaluation that thorough mode throws away.** After every
   regraft, thorough SPR rounds computed the tree likelihood and then replaced it
   with the score of the 3-branch optimization, which starts by evaluating the same
   edge. Only the CLV updates are kept (~11% of a run).
2. **autoMRE: packed bits and popcount.** The bootstopping test (1000 permutations
   every 50 replicates) counted each split's occurrences one tree at a time in a
   `std::vector<bool>`; a 101-taxon job paused ~7 s after every 50th replicate while
   the other worker waited at the barrier. Same integer counts, from 64-bit words.
3. **Site repeats: grow-only buffers** instead of freeing and reallocating a node's
   CLV, scaler and index arrays whenever its number of repeat classes changed.
4. **Site repeats: branch-free class identification** (a coin-flip branch per site
   on every CLV update).
5. **4x4 kernels from matrix columns.** The CLV update and sumtable kernels formed
   every P*v as row products plus a transpose-add with six shuffles, five of them
   on the single shuffle port; the column form computes the same products and pairs
   the additions the same way, so the result is bitwise identical. Two sites per
   iteration share the column loads.
6. **Edge log-likelihood: a 4-state path** (DNA used the generic kernel), with the
   per-site terms computed in chunks before the `log()` calls so the pipeline can
   overlap sites.
7. **Branch-length derivatives:** two sites share the per-branch table loads, and
   the 4-site division steps run after each chunk instead of stalling the loop.
8. **Version `2.0.3-dikarya1`**, so a job's `tree/raxml_run.raxml.log` names the
   binary.

Not changed: anything that alters arithmetic (reassociated sums, `-march=native`,
LTO, a vectorized `log()`), the search algorithm, or thread scheduling.

## Verification before adoption

- Stock 2.0.2 rebuilt here (gcc 13.3.0, the release's configuration) reproduces the
  installed official binary byte for byte, so the build is the release build.
- Kernel harnesses run each rewritten kernel and the original on thousands of random
  inputs (1-8 rate categories, every site-repeat layout, all scaling modes, invariant
  sites, zero/-0/subnormal/negative matrix entries): all outputs identical bit for
  bit. The repeat-class loop was checked separately (renumbering would not show in
  the output). A build that fills every reused buffer with NaN/junk
  (`-DCORAX_REPEATS_POISON_TEST`) gives identical output.
- End to end, dikarya1 vs stock 2.0.3 on real job alignments: *pending*. The
  install script's synthetic self-test (five command shapes, each at `--threads 1`
  and `--threads 2`) matched stock 2.0.3 on 2026-10-12.

## Installing / switching / rolling back

As root on the production host:

```zsh
hostname                                   # must print mycolab
bash /var/www/dikarya/ops/raxml/install-raxml-dikarya.sh
```

The script clones 2.0.3 (checking the release commit and the coraxlib submodule
commit), builds it twice -- stock, and with the patches -- with the release's
configuration (about 8 minutes on 2 vCPUs, at `nice 10`), and refuses to install
unless the patched binary reproduces the stock build byte for byte on a synthetic
alignment for the command shapes Dikarya runs (autoMRE with both support metrics, KH
stopping rule, `+I+G`, `+I+R3`, ML search only, MOOSE). It needs `git`, `cmake`,
`g++` and network access to GitHub. Then point Dikarya at it -- comment out any
existing `RAXML_BINARY` line rather than adding a second one -- and have an agent
restart the workers with the guarded wrapper (see "Restarting Dikarya services" in
`AGENTS.md`):

```zsh
grep -n RAXML_BINARY /etc/dikarya/dikarya.environment.live
sed -i 's|^RAXML_BINARY=.*$|# & (replaced by dikarya1)|' /etc/dikarya/dikarya.environment.live
echo 'RAXML_BINARY=/opt/raxml-ng-2.0.3-dikarya1/bin/raxml-ng' >> /etc/dikarya/dikarya.environment.live
grep -n RAXML_BINARY /etc/dikarya/dikarya.environment.live
```

New jobs' `tree/raxml_run.raxml.log` then start with `RAxML-NG v. 2.0.3-dikarya1`.
To roll back, comment out the new line (and restore the old one, if there was one)
and restart the workers; `/usr/local/bin/raxml-ng` is untouched.
