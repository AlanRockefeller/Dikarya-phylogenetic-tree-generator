# MrBayes build used by Dikarya

Dikarya runs MrBayes for the "MrBayes" tree method through `_run_mrbayes()` in
`app/services/tree_builder_service.py`, with the binary taken from `MRBAYES_BINARY`:
GTR-family models with `+G`, `+I` or `+I+G`, two runs of four chains, up to a
million generations with the split-frequency stop rule. MrBayes jobs are rare (53
on disk, 7 in the 30 days before 2026-10-09) but long: a 129-taxon job ran
1,000,000 generations in 33 minutes, and two 126/134-taxon jobs died after 3-4
minutes on a NaN likelihood (Dikarya's +I+G retry exists for them).

| build | path | what it is |
|---|---|---|
| `3.2.7a` | `/usr/bin/mb` | Ubuntu's package, using Ubuntu's libhmsbeagle 3.1.2 |
| `3.2.9-dikarya2` | `/opt/mrbayes-3.2.9-dikarya2/bin/mb` | MrBayes 3.2.9 plus `mrbayes-3.2.9-dikarya2.patch`, with its own BEAGLE 4.0.1 plus `beagle-4.0.1-dikarya2.patch` in `/opt/mrbayes-3.2.9-dikarya2/lib` |

**MrBayes does its likelihoods in BEAGLE.** Ubuntu's `mb` is built with BEAGLE and
uses it by default: every job log says `Using BEAGLE v3.1.2 ... Impl Name :
CPU-4State-Single`. About three quarters of a run is in BEAGLE's CPU kernels, so
the build ships with its own patched library rather than Ubuntu's.

**3.2.9 is an upstream change in its own right.** It is the newest MrBayes source:
the `release/v3.2.9` branch at `4f741bc` (2026-09-29; not tagged yet), which is the
3.2.8 release (2026-09-18) plus one fix, the NodeSlider proposal ratio for non-clock
trees ("introduced in v3.2.4 by mistake"). Nodeslider is 7 of the 52 proposal
weights in every Dikarya analysis, and the old ratio favoured longer branch pairs.
So 3.2.9 results differ from today's 3.2.7a runs for that reason alone. BEAGLE
4.0.1 (the newest release), built as Ubuntu builds it, gives the same bits as
Ubuntu's 3.1.2.

**dikarya2 gives the same results, faster.** For the same input, options and seed,
every output file -- sampled parameters and trees, `.mcmc` diagnostics, checkpoints,
`sump`/`sumt` summaries -- and stdout apart from timings, the run stamp and version
banners are byte-identical to stock MrBayes 3.2.9 with stock BEAGLE, at one thread
and at two. That includes runs that end on `Log likelihood nan`: the build keeps
Ubuntu's `--enable-debug` configuration, whose run-time checks are what turn a NaN
into the abort `_run_mrbayes` catches.

## Measured on this host (Xeon E5-2690 v4, 2 vCPUs)

Real Dikarya MrBayes inputs with Dikarya's production settings (GTR+G, 2 runs x 4
chains, samplefreq 500, stop rule), a fixed number of generations, the builds
interleaved (ABBA), each run on a quiet host (it waited until the rest of the host
was idle, and was repeated if anything else took more than a quarter of a CPU while
it ran), wall time:

| input | taxa x site patterns | generations | Ubuntu 3.2.7a | stock 3.2.9 | dikarya2, 1 thread | dikarya2, 2 threads |
|---|---|---|---|---|---|---|
| job yb4s | 36 x 394 | 100,000 | 78.5 s | 85.3 s | 45.5 s (1.73x) | 28.8 s (2.72x) |
| job 27d6 | 34 x 342 | 100,000 | 60.3 s | 65.8 s | 37.7 s (1.60x) | 23.5 s (2.56x) |
| job tr9b | 129 x 209 | 30,000 | 54.3 s | 59.7 s | 33.5 s (1.62x) | 21.0 s (2.58x) |
| job 1nek | 134 x 195 | 30,000 | 65.5 s | 64.2 s | 37.1 s (1.77x) | 24.3 s (2.69x) |
| alignment s33 | 240 x 564 | 6,000 | 47.3 s | 47.3 s | 27.3 s (1.73x) | 19.3 s (2.44x) |
| job cad46114 | 188 x 1191 | 3,000 | 34.6 s | 36.0 s | 21.3 s (1.62x) | 15.1 s (2.29x) |

Speed-ups are against Ubuntu's `mb`, which is what Dikarya runs today. Against stock
3.2.9 built the same way, one thread is 1.69-1.88x faster and two threads 2.39-2.96x.
At this rate the 129-taxon job that took 33 minutes would take about 13 minutes on
the two-thread worker and about 20 on a one-thread one.

Dikarya gives MrBayes the worker's thread budget: `dikarya-worker` has two threads
(`DIKARYA_WORKER_THREADS=2`), `dikarya-worker-high2` and `dikarya-worker-bulk` one,
so a job takes the two-thread column or the one-thread column depending on which
worker runs it. Two threads use about 1.6 times the memory (each chain keeps its own
scratch buffers): 175 MB instead of 109 MB for a 240-taxon alignment.

## What the patches change

`beagle-4.0.1-dikarya2.patch` (5 commits) and `mrbayes-3.2.9-dikarya2.patch`
(4 commits); each commit message gives the exactness argument and the measurement.

1. **BEAGLE built without `-march=native`.** Upstream's CMake forces it, and g++
   then fuses multiplies and adds into FMAs (even with `-std=c++11`), which changes
   single-precision results; Debian and Ubuntu build without it. The stock reference
   in the install script gets this patch too.
2. **AVX2 kernels for BEAGLE's single-precision 4-state CPU code** (partials from two
   partials, a tip and partials, two tips, with and without fixed scaling; the edge
   likelihood; rescaling): two patterns per 256-bit vector, every value the same
   IEEE operations on the same operands in the same order, never an FMA.
3. **No subnormal assists.** On large trees, partial likelihoods between MrBayes's
   scaler nodes routinely become subnormal, and every float multiply that touches
   one costs a ~120-cycle microcode assist: half the kernel time on a 129-taxon job.
   Each block is first computed with flush-to-zero on; if the underflow flag stays
   clear the result is provably IEEE, otherwise the block is redone with every risky
   multiply in double (one correctly rounded product, as the float multiply). Which
   path runs never changes a bit, only the time.
4. **Chains computed concurrently** (MrBayes patch 3, BEAGLE patch 4). With
   `set beaglethreadcount=2` -- which `_run_mrbayes` writes when the worker's budget
   is two -- each generation's eight likelihoods are computed two at a time instead
   of one after the other. Every random number is still drawn on the main thread in
   MrBayes's order (a chain's move and its acceptance draw together; each swap's one
   draw reserved in its place), so it is the same run: each chain has its own scratch
   buffers and a private copy of the division's settings, BEAGLE keeps per-thread
   scratch, and the one piece of scratch MrBayes really shares between chains (the
   scaler-node flags used by numerical rescaling, which it reads stale) is still
   handled on the main thread in chain order. The build measures its generations per
   second with two threads and with one and uses the faster: next to another busy
   job, two threads that keep waiting for each other lose to one.
5. **Exact pattern threading** where chains are not computed concurrently (a single
   chain, or a run that does not qualify): `set beaglethreadcount=N` splits each
   likelihood call by site pattern, summing the site log likelihoods afterwards in
   pattern order, so results do not depend on N. It engages only from 768 site
   patterns. BEAGLE's own threading, which sums per partition and so changes
   results, is not used.
6. **MrBayes: parsimony-biased moves** (ParsSPR, ParsTBR and the Fitch passes they
   need) in AVX2: exact set algebra, and integer sums of the integer site weights.
7. **MrBayes: `log(branch length)` cached per node** for the gamma-Dirichlet branch
   length prior, which summed it over every branch twice per move.
8. Versions `3.2.9-dikarya2` and `4.0.1-dikarya2`, so a job's
   `logs/tree_builder.log` names the build. MrBayes computes chains concurrently only
   on a BEAGLE that says dikarya2.

Not changed: MrBayes's build configuration (Ubuntu's flags, `--enable-debug`), the
MCMC, the scaling scheme, anything that alters arithmetic.

## Verification before adoption

- A rebuild of 3.2.7a from source with Ubuntu's configuration reproduces the
  installed `mb` byte for byte, and BEAGLE 4.0.1 built the same way reproduces
  Ubuntu's 3.1.2 (88 of 88 suite runs identical), so the references are the distribution's
  builds.
- Kernel harness: the stock and patched BEAGLE kernel bodies, extracted from both
  source trees and compiled side by side, agree bit for bit on more than 120,000 random
  cases (values from 1e-45 to 1, zeros, -0, subnormals, infinities, NaN, negative
  tip states, odd pattern counts and sub-ranges); breaking the exact path by one ulp
  is caught.
- End to end, 44 real Dikarya alignments (7 to 240 taxa) x 2 of the model/chain
  settings `_run_mrbayes` writes, against stock 3.2.9 with stock BEAGLE: 88 of 88
  identical at one thread; 88 of 88 identical at two threads (chains concurrent where
  the layout allows, pattern threading forced on elsewhere), with the binary the
  install script produced.
- ThreadSanitizer (both libraries instrumented) on five two-thread runs, 23 to 240
  taxa, +G, +I+G and equal rates, 2x2, 2x4 and 3x3 chains: no races.
- The two real jobs that failed on a NaN, run at full length at two threads: stock
  and dikarya2 both stop with `Log likelihood nan after move 'Dirichlet(Revmat)'` at
  the same generation with identical output.

## Installing / switching / rolling back

As root on the production host:

```zsh
hostname                                   # must print mycolab
bash /var/www/dikarya/ops/mrbayes/install-mrbayes-dikarya.sh
```

The script clones MrBayes (checking commit `4f741bc` on `release/v3.2.9`) and BEAGLE
`v4.0.1` (checking `bf35ad4a`), builds each stock and patched in Ubuntu's
configuration (about 8 minutes on 2 vCPUs, at `nice 10`) under
`/usr/local/src/mrbayes-3.2.9-dikarya2`, and installs into
`/opt/mrbayes-3.2.9-dikarya2` only if the patched build reproduces the stock one
byte for byte on a synthetic 96-taxon alignment for every block shape Dikarya
writes (models, chain layouts, the stop rule, one and two threads), then checks
that the installed binary loads its own library. It needs `git`, `cmake`,
`gcc`/`g++` and network access to GitHub. Then point Dikarya at it -- comment out
any existing `MRBAYES_BINARY` line rather than adding a second one -- and have an
agent restart the workers with the guarded wrapper (see "Restarting Dikarya
services" in `CLAUDE.md`):

```zsh
grep -n MRBAYES_BINARY /etc/dikarya/dikarya.environment.live
sed -i 's|^MRBAYES_BINARY=.*$|# & (replaced by dikarya2)|' /etc/dikarya/dikarya.environment.live
echo 'MRBAYES_BINARY=/opt/mrbayes-3.2.9-dikarya2/bin/mb' >> /etc/dikarya/dikarya.environment.live
grep -n MRBAYES_BINARY /etc/dikarya/dikarya.environment.live
```

New jobs' `logs/tree_builder.log` then show `MrBayes 3.2.9-dikarya2` and
`Using BEAGLE v4.0.1-dikarya2`. To roll back, comment out the new line (and restore
the old one, if there was one) and restart the workers; `/usr/bin/mb` is untouched.

`_run_mrbayes` also writes `set beaglethreadcount=N;` (the worker's thread budget,
when above 1) before `set seed` -- every `set beagle...` command reruns MrBayes's
analysis setup, which draws from the seeded random stream, so after the seed it
would change the run; before it the results are identical to a run without it.
Ubuntu's `mb` accepts the command and, on this two-CPU host, does not thread.
