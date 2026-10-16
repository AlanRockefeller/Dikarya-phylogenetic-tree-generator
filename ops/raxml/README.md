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
2.0.3. One qualification at `--threads 2`, where stock differs from itself run to
run: the two workers' log lines interleave in a different order, and MOOSE lists its
models in the order they finish. So `compare-raxml-runs.py` compares each worker's
lines in its own order and MOOSE's model lines as a set. The values must still match
exactly.

## Measured on this host (Xeon E5-2690 v4, 2 vCPUs)

Real Dikarya alignments from `var/jobs`, each run with the job's own model, seed,
starting trees and support options (inputs rebuilt exactly as `_run_raxml` does:
replaying a job's command reproduces its stored trees byte for byte). Only the
bootstrap count was cut where a full run would take hours; dd7c1be7 is the complete
job. Stock 2.0.3 against a build made from the patches in this directory,
interleaved (ABBA, two runs each), one input per hold of the benchmark lock, on
2026-10-16. Every run's output was identical to stock.

| job | taxa x patterns | model | run | stock 2.0.3 | dikarya1 | speedup |
|---|---|---|---|---|---|---|
| n0k5 | 127 x 153 | HKY+FE+R2 | 20 starting trees, 20 bootstraps | 59.2 s | 46.0 s | 1.29x |
| fzc7 | 101 x 218 | GTR+G | 20 starting trees, 20 bootstraps | 99.0 s | 69.8 s | 1.42x |
| b930451f | 75 x 449 | TIM1+FO+I+R3 | 20 starting trees, 10 bootstraps | 124.1 s | 89.6 s | 1.39x |
| c91f0a5a | 240 x 588 | HKY+FO+R3 | 20 starting trees, 4 bootstraps | 653.8 s | 481.2 s | 1.36x |
| dd7c1be7 | 40 x 189 | GTR+G | the whole job: 5 starting trees, autoMRE{500} | 154.3 s | 108.8 s | 1.42x |
| b4a1f45e | 430 x 495 | TVM+FO+I+R4 | 1 starting tree, 10 bootstraps, KH rule | 334.1 s | 241.8 s | 1.38x |
| 0be9b363 | 136 x 229 | MOOSE's choice | 2 starting trees, autoMRE{100}, KH rule | 277.4 s | 185.8 s | 1.49x |
| all seven, `--threads 1` (CPU time) | | | | 1702 s | 1223 s | **1.39x** |
| fzc7, `--threads 2` (wall time) | | | | 53.5 s | 37.7 s | 1.42x |
| dd7c1be7, `--threads 2` (wall time) | | | | 99.1 s | 66.6 s | 1.49x |

`--threads 2` is how the main worker runs RAxML: two coarse-grained workers, each
running the same single-threaded searches, so the gain carries over. On the 88 hours
of RAxML in the job history, 1.39x would have saved about 25 hours, before the
bootstrapping changes below.

## Bootstrapping settings (2026-10-16)

Two changes to how Dikarya runs RAxML-NG alter results slightly, so unlike the
patches they were measured on what they change, not only on speed. Both apply to
any `RAXML_BINARY`.

**Identical sequences are left out of the tree search.** `_run_raxml` gets an
alignment with one sequence per group of identical sequences. After the run, each
group goes back onto the delivered trees (FBP and TBE) as a zero-length polytomy
where its kept sequence sits, with no support value on the clade that joins them
(`app/services/identical_sequences.py`). The same applies to the advanced IQ-TREE
method and to MrBayes. Six production jobs with duplicates, each replayed with its
own model, seed and starting trees (bootstrap count reduced so a pair of runs takes
minutes), CPU time on the full alignment and on the collapsed one:

| job | taxa | identical | full | collapsed | saved | log-likelihood change | RF distance |
|---|---|---|---|---|---|---|---|
| fzc7 | 101 | 12 (12%) | 74.2 s | 64.0 s | 13.7% | -0.002 | 0.30 |
| b930451f | 75 | 10 (13%) | 104.7 s | 89.5 s | 14.6% | +0.048 | 0.02 |
| 602ab439 | 179 | 49 (27%) | 140.2 s | 93.0 s | 33.6% | -0.711 | 0.59 |
| n0k5 | 127 | 35 (28%) | 54.9 s | 38.8 s | 29.4% | +0.474 | 0.45 |
| 3235d144 | 341 | 102 (30%) | 115.1 s | 70.0 s | 39.2% | -0.062 | 0.50 |
| 1vpz | 140 | 47 (34%) | 65.0 s | 39.7 s | 39.0% | +0.002 | 0.51 |

The log-likelihood change is that of the re-added tree, scored with
`raxml-ng --evaluate` on the full alignment, against the tree from the full
alignment. The RF distance (normalized) compares the two topologies with one tip
per identical group. Rerunning the full alignment with a different seed moves these
jobs just as far: RF 0.33-0.74 and up to 0.5 log-likelihood units. These ITS
datasets have many near-equal trees. Applied to the 92 finished RAxML jobs on disk,
the collapse would have saved about 8-9% of all RAxML wall time (7-8 of 88 hours),
almost all of it in jobs with many duplicates.

**The AutoMRE cutoff defaults to 0.07** (`--bs-cutoff`; RAxML-NG's own default is
0.03), with a dropdown in the advanced options (0.03 Slowest, 0.04 Slow,
0.07 Medium, 0.10 Fast). The convergence test was replayed on the stored bootstrap
trees of 67 production jobs (81.7 hours of bootstrapping, 9,051 branches),
using a build of RAxML-NG's own `--bsconverge` test that draws its permutations
from the run's seed as the in-run test does. At 0.03 the replay reproduces each
job's actual stopping point in 64 of 67 jobs (the other three are within one
50-replicate check). Support values at each cutoff, against the job's full run at
0.03:

| cutoff | bootstrap time saved | mean support change | largest | branches >=70 that drop below | <70 that rise above | >=95 that drop below |
|---|---|---|---|---|---|---|
| 0.04 | 40% | 0.71 | 7 | 33 of 3,209 | 34 | 31 of 1,684 |
| 0.05 | 63% | 1.22 | 11 | 60 | 79 | 48 |
| 0.07 | 80% | 1.89 | 16 | 100 | 109 | 76 |
| 0.10 | 90% | 2.84 | 21 | 113 | 183 | 120 |

For scale: two complete independent runs of the same job at 0.03 (different seeds)
differ by 1.4-1.8 support points on average and by up to 6.

Evaluated and not adopted: educated bootstrap guessing (`--ebg`, a predictor trained
on parsimony features). Against the real Felsenstein supports of 56 jobs (7,947
branches) it was off by 9.5 points on average (biased 3.6 low, worst 83), and it
put 19% of branches with FBP >= 70 below 70, in a median 4 seconds. Users who want
fast support have Quick Tree's SH-aLRT.

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
- End to end, dikarya1 vs stock 2.0.3 on real job alignments, each with the job's own
  model, seed, starting trees and stopping rule: all identical. That covers 18 jobs at
  `--threads 1` (6-430 taxa; GTR, HKY, TIM, TPM and TVM models with `+G`, `+I` and
  `+R2..R5`, three of them chosen by MOOSE; fzc7 complete with its 1000-replicate
  cap, the larger jobs with fewer replicates or starting trees), 3 at `--threads 2`
  with coarse-grained workers, 2 with one worker on two threads (fine-grained), and
  MOOSE on 2 jobs. The timing runs above were compared the same way, each against
  stock.
  The install script's synthetic self-test (five command shapes, each at
  `--threads 1` and `--threads 2`) also passes.

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
