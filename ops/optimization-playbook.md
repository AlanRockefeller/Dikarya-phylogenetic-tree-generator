# Playbook: speeding up a bioinformatics tool on the Dikarya server

Written 2026-10-07 after the MAFFT work (`ops/mafft/`, ~3.7x over the Ubuntu package with
byte-identical output). Read this before optimizing RAxML-NG, IQ-TREE, MrBayes or
FastTree. It records the method, this host's quirks, and what worked and did not.

## 1. Pick the target by what users actually run

Tree methods of jobs created in the 30 days before 2026-10-07 (`tree_method` in
`var/jobs/*/input_info.json`):

| method | jobs | binary (Config key) |
|---|---|---|
| `iqtree_fast` (Quick Tree) | 1215 | `/usr/local/bin/iqtree3` 3.1.4, static (`IQTREE_BINARY`) |
| `fasttree` | 204 | `/usr/local/bin/FastTree` (`FASTTREE_BINARY`) |
| `iqtree` | 51 | `iqtree3` |
| `raxml` | 11 | `/usr/local/bin/raxml-ng` 2.0.2, static (`RAXML_BINARY`) |
| `mrbayes` | 7 | `/usr/bin/mb` 3.2.7a, Ubuntu package (`MRBAYES_BINARY`) |

Re-run the count before choosing; it moves. Total CPU time matters more than job count,
so also look at `event=job.*` durations in the worker logs or the digest
(`scripts/dikarya_log_digest.py`) for which tool burns the most hours. A rare tool that
runs for hours can still matter (RAxML/MrBayes hold a worker slot far longer).

How Dikarya calls each tool lives in `app/services/tree_builder_service.py` (presets,
flags, `_run_iqtree(..., fast=True)` for Quick Tree) and the thread budget in
`app/services/alignment_service.py: worker_thread_budget()`: the main `phylo_high`
worker runs with 2 threads, `high2` and `bulk` with 1 (`DIKARYA_WORKER_THREADS` in the
units). Benchmark with the exact command line a real job used (`logs/tree_builder.log`).

## 2. Cheap checks before touching code

RAxML-NG and IQ-TREE already ship hand-written SSE/AVX/AVX2 likelihood kernels, and
MrBayes can use BEAGLE. Expect smaller wins than MAFFT, whose x86 fast paths were
AVX-512-only and which had plainly inefficient hot loops. Check, in this order:

1. **How was the installed binary built?** A generic static release may pick its SIMD
   level at run time (fine) or be compiled for a baseline CPU (a rebuild with
   `-march=native` and the tool's own AVX2 switch can be a free win). The Ubuntu `mb`
   is a generic distro build -- check whether it was built with SSE/AVX and BEAGLE.
2. **Does it pick the right kernel here?** RAxML-NG `--simd`, IQ-TREE `-v` / log lines
   name the vectorization used. MrBayes: `set usebeagle`.
3. **Threads.** Compare 1 vs 2 threads on real inputs; for small alignments threading
   overhead can lose. Never exceed the worker's budget.
4. **Only then profile** (section 4) and consider source changes.

## 3. Ground rules (what made the MAFFT result trustworthy)

- **Same output, faster.** For the same input, options and seed, the patched build must
  produce byte-identical results to the stock build compiled the same way. Phylogenetics
  tools are seeded and floating-point: fix the seed (`--seed`, `-seed`, `set seed`),
  compare tree files and logs' likelihoods byte for byte at 1 thread. If a change cannot
  be exact (reordered floating-point sums), it is a different result -- stop and ask the
  user before going down that road.
- **Rounding.** Whether gcc fuses `a*b+c` into an FMA (with FMA available, e.g.
  `-march=native`) is decided by the effective `-ffp-contract`, not by `-std=` alone.
  Measured with gcc 13.3 at `-O2 -march=native`: C in ISO mode (`-std=c99`) does not fuse,
  C in GNU mode (`-std=gnu11`, the default) does, an explicit `-ffp-contract=fast` or
  `=off` overrides either, and g++ fuses even under `-std=c++11`. `gcc -Q --help=optimizers`
  reports `fast` for `-std=c99` regardless, so do not trust it: compile a one-line
  `a*b+c` with the build's real flags and grep the assembly for `vfmadd`. Record the
  effective setting with the measurements, match the stock build's, and in vector code
  use a separate multiply and add unless the stock build fuses.
- **Reference builds.** Build stock and patched side by side from the same commit with the
  same compiler and flags, in the scratchpad. Also keep the currently installed binary as
  a third reference.
- **Real inputs.** Sample `var/jobs/*/alignment/alignment_trimmed.fasta` (gzipped at rest:
  use `app/services/artifact_storage.py` helpers or `gzip -dc`) stratified by size and
  method. Never commit or push user data -- benchmark inputs stay in the scratchpad;
  anything published uses synthetic data (a fixed-seed awk generator works).
- **Skipping initialization needs a poison test.** If you replace `calloc` with `malloc`
  or drop a `memset`, add a `-D..._POISON_TEST` build that fills the memory with junk and
  re-run the full identity suite.
- **Verify at three levels:** a quick 3-5 input check per change, the full ~50-input suite
  before committing, and a cross-option harness (every mode the tool offers that Dikarya
  uses) before publishing.

## 4. This host

- Intel Xeon E5-2690 v4 (Broadwell-EP), **2 vCPUs, AVX2 + FMA, no AVX-512**, a VM.
- **No hardware counters** (`perf stat` says cycles "not supported"). `perf record` still
  works for sampling but needs `kernel.perf_event_paranoid=2` (the user sets it as root:
  `sysctl -w kernel.perf_event_paranoid=2`, revert with `=4`) and must run **outside the
  sandbox** (`dangerouslyDisableSandbox`), or it records nothing and the program gets no
  output. Build with `-g` for symbols; `make install` strips, so profile the build tree.
- For trustworthy per-function timing, instrument with `__rdtsc()` (TSC runs at 2.6 GHz
  nominal; turbo ~3.2 GHz) and accumulate around the hot call, reporting from a
  destructor to a file named by an env var (stderr is often swallowed by wrapper scripts).
- Port limits that shaped the MAFFT kernels: on Broadwell every cross-lane shuffle
  (`vpermd`, `vperm2i128`, `palignr` ymm) and `vpblendvb` issue on port 5 only; vector
  add/max/cmpgt on ports 1 and 5; logic and `vpblendd` on 0/1/5. Prefer in-lane
  `vpslldq` + OR-ed fill, `vpblendd`, and and/xor blends.
- The CPU sustains ~3.7 independent ymm adds per TSC cycle; if a kernel gets far less,
  count uops per element before blaming latency or memory.
- Production shares the box: check `uptime` and the RQ queues before timing, interleave
  stock/patched runs per input, and use `nice -n 10`.

## 5. Session mechanics that bit us

- Background jobs started with `&` or `nohup` inside a Bash call die when the call
  returns. Use `run_in_background: true`, then wait on its notification.
- The interactive shell is zsh: `$var` does not word-split (use `$=var` or a bash script),
  and a glob with no match is an error (`rm -f gmon.*` aborts the line).
- `pkill -f <pattern>` matches the shell running it if the pattern is in its own command
  line -- kill by PID.
- `cp -r` of a build tree gives every file a fresh mtime, so `make` may skip recompiling
  with new flags: `make clean` first.
- The user's root shell may be on a different machine (`xps17`); production is `mycolab`.
  Root command blocks should start with `hostname`.
- Redis and `perf` need the sandbox disabled; scratch builds do not.
- Never run a tool with an unpacked LLVM toolchain's `lib/` on `LD_LIBRARY_PATH`.
  IQ-TREE's static binary (official one included) has LLVM's OpenMP runtime linked in;
  at startup it looks for OpenMP tool plugins (`libompd.so`, `libarcher.so`), loads the
  toolchain's newer ones and spins at 100% CPU forever, printing nothing. Give the
  toolchain's libraries to the compiler only.
- Each sandboxed Bash call has its own PID namespace: `ps` cannot see a process started
  by a background task, so watch its output files instead.
- Bash reads a script as it runs it: editing a running benchmark script breaks that run.
  Copy it, or wait.
- `pid=$(cmd & echo $!)` never returns if `cmd` keeps the pipe open: redirect its output.
- `git clone` from GitHub inside the sandbox needs `github.com` declared for the command.

## 6. What worked on MAFFT (for ideas, not as a recipe)

- Profile first, every time: the biggest win (`Falign`'s `calloc`, a quarter of FFT-NS-i)
  and the second (a 26x26 loop compiled into gathers) were invisible in the code until
  profiled.
- **Change the work, not just the instructions.** The straight AVX2 port of the hot DP
  loop gave 1.17x; storing markers in the traceback matrix and recomputing the few
  needed offsets during traceback gave 2x.
- **Exact algebraic shortcuts** are fine when provably exact (skipping `+0` terms in a sum
  that can never be `-0`).
- **Dead ends:** wider vectors when the loop is uop-bound; "lazy" branches that are taken
  too often on related sequences (gaps win across large areas of ITS alignments);
  restructuring for latency when the problem is throughput.

## 6b. What worked on IQ-TREE (2026-10-08, `ops/iqtree/`)

- **Match the release's toolchain before anything else.** The official 3.1.4 binary is
  clang 22 + glibc 2.35 static (the "GNU CC 11.4" string in it is glibc's banner), with
  IQ-TREE's bundled libomp/zlib and Ubuntu 22.04's Eigen/Boost headers. A clang-22
  stock rebuild reproduced the release byte for byte, which made the release itself the
  identity reference. gcc would not: it contracts FMAs differently in the FMA kernel.
- **Replaying a job proves the inputs.** `alignment/alignment_trimmed.fasta` renamed with
  `sanitize_fasta_headers()` plus the job's `--seed` and `-T 1` reproduces the job's own
  `iqtree_run.treefile`; do that before trusting a benchmark set.
- **Most of the win was work the program did not need to do**, not faster arithmetic:
  SH-aLRT re-drew identical bootstrap replicates for every branch (~20% of Quick Tree);
  UFBoot printed every candidate tree through iostreams before checking whether any
  replicate wanted it (~27% of a UFBoot run) and re-serialized all 1000 trees into the
  checkpoint after each refined tree (~10%), though the file is written once a minute.
- **Store-forwarding stalls hide in "data movement".** The tip-state loaders built each
  256-bit vector from four scalar stores; a 4x4 in-register transpose was 7-11%.
- **The kernels themselves are near their limit** for exact output: the FP and load
  ports are both busy, so sharing coefficient broadcasts or wider vectors buys little.
- **Things that do not help or are not allowed:** PGO (3% on Quick Tree, 0% on UFBoot,
  not worth a 7.7 MB profile); `-march=native` on the generic files (Eigen and
  vectorclass switch packet width and FMA on `__AVX2__`/`__FMA__`, which changes
  results); LTO (could inline a non-FMA `a*b+c` into an FMA function and fuse it).
- `-T 2` runs are not reproducible run to run even in stock (dynamic scheduling,
  per-thread RNG streams), so multi-threaded logic was checked with a debug build that
  recomputes and compares (`-DIQTREE_ALRT_CACHE_CHECK`) instead of by output bytes.
- An OpenMP region with one thread still costs a runtime call (~5% of a UFBoot run in
  `__kmp_fork_call`/serialized-parallel); a direct loop for `num_threads == 1` is exact
  when the reduction copies start at +0.0.

## 6c. What worked on RAxML-NG (2026-10-09, `ops/raxml/`)

- **Toolchain:** the official static binaries are Ubuntu 24.04 gcc 13.3.0, i.e. this
  host's system compiler; `-DSTATIC_BUILD=ON -DPORTABLE_BUILD=ON` reproduced the
  installed 2.0.2 byte for byte. The identity reference was stock 2.0.3 (newer than
  production, and 2.0.3 changes results by itself), built with the same flags.
- **Replaying a job:** a job's `raxml_run.raxml.rba` is kept but carries a model, so
  a replay from it is close, not exact. Rebuild `raxml_input_sanitized.fasta` from
  `alignment/alignment_trimmed.fasta` (renamed `SEQ%06d` in order, as
  `sanitize_fasta_headers()` does) and rerun the logged command: exact. Some jobs'
  alignments were later grown by "add sequences" -- check the taxon count.
- **Where the time goes:** bootstrap replicates (~95%), i.e. thorough SPR rounds:
  local 3-branch Newton optimization (derivatives + sumtables) and CLV updates with
  site repeats. Coarse-grained `--threads 2` = 2 workers x 1 thread.
- **Work not needed:** a likelihood evaluated after every regraft and overwritten by
  the branch optimizer's own first evaluation (~11%); an autoMRE test counting bits
  one tree at a time in `vector<bool>`; per-update free/malloc of site-repeat
  buffers; small per-call heap tables.
- **Exact kernel rewrites that paid:** 4x4 `P*v` from matrix columns (same products,
  same addition pairing as the row-product + transpose-add, without five port-5
  shuffles); two sites per iteration sharing loads -- with `__asm__("" : "+x"(v))`
  to stop gcc folding the shared load into each FMA as a memory operand; and
  **chunked two-phase loops** where a per-site chain ends in something slow (`log()`,
  a 4-wide division): compute 64 sites' terms first, then the slow step back to back.
  Those latency-bound loops gained 1.5-1.7x without changing an operation.
- **Traps:** gcc turns `c ? a : b` back into an unpredictable branch -- write a mask
  select and check the disassembly. GCC's `_mm256_mul_pd`/`_add_pd` are plain vector
  arithmetic, so in an `-mfma` file they can be contracted like scalar code.
  `fma(a,b,0)` is not `0 + a*b` (an underflowed product stays -0), while
  `canon(a)+canon(b) == canon(a+b)` for `canon(x) = x+0` holds for all doubles.
  End-to-end output cannot detect a renumbering of site-repeat classes (the
  likelihood does not depend on it): test such bookkeeping with a unit check.
  Random harness data with subnormals ran 3x slower than real data; time kernels on
  realistic values. Search-mode logs prefix lines with `[hh:mm:ss logLH]` and MOOSE
  JSON carries timing metadata -- the comparer must strip those and nothing else.
- **Kernel harness:** compile the stock and patched kernel file with coraxlib's own
  flags, rename the stock object's globals (`nm --defined-only -g` +
  `objcopy --redefine-syms`, prefix `ref_`), link both against stock `libcorax.a`,
  and compare outputs bit for bit on thousands of random cases (every repeat layout,
  scaling mode, rate count, zero/-0/subnormal/negative inputs).
- **Another user's jobs** shared the 2 vCPUs for hours: profiles taken at different
  times were not comparable at all; only ABBA runs and same-run ratios were.
- **Bigger wins came from settings, measured on what they change** (2026-10-16):
  leaving identical sequences out of the search (14-39% on jobs with duplicates)
  and an AutoMRE cutoff of 0.07 (80% of bootstrap time). These change results, so
  each was judged against the run-to-run noise of full runs with different
  seeds, never against zero. The cutoff needed no new runs: the jobs' stored
  `.raxml.bootstraps` files replay the stopping test at any cutoff, but only with a
  build whose `--bsconverge` draws its permutations from `--seed`. Stock uses
  `rand()` there, unlike the in-run test, and so does not reproduce the job's own
  stopping point. Score a tree with re-added duplicates on the full alignment with
  `--evaluate` (re-add them on 1e-6 branches there, since `--evaluate` reoptimizes
  lengths anyway).

## 6d. What worked on MrBayes (2026-10-09, `ops/mrbayes/`)

- **Find the likelihood engine first.** Ubuntu's `mb` is built with BEAGLE and uses
  it by default (`Using BEAGLE ... CPU-4State-Single` in every job log), so ~75% of a
  run is in `libhmsbeagle-cpu`, not MrBayes. The deliverable is MrBayes plus its own
  BEAGLE build, installed together. BEAGLE 4.0.1 built *without* `-march=native`
  gives the same bits as Ubuntu's 3.1.2; upstream's CMake forces `-march=native`,
  and **g++ contracts `a*b+c` into an FMA even with `-std=c++11`** (gcc only stops
  for ISO C), so that build differs.
- **Keep the distribution's build mode.** Ubuntu builds MrBayes `--enable-debug`
  (no `-DNDEBUG`). The checks that compiles in include the `Log likelihood nan`
  abort that `_run_mrbayes` turns into its +I+G retry; a release build silently
  rejects the NaN move instead. Short identity runs never hit it: test the real
  failing jobs at full length (jagz, 1nek abort near generation 200,000).
- **`set beagle...` after `set seed` changes the run** (each one reruns the analysis
  setup, which draws from the seeded stream). Put such settings before the seed.
- **Subnormal assists were half the kernel time on large trees.** On Broadwell a
  float multiply with a subnormal input or result costs ~120 cycles (a divide with a
  subnormal numerator ~150); adds, max and compares cost nothing. Exact fix: run a
  block with flush-to-zero on and the underflow flag cleared; a clear flag proves
  the result is IEEE. Otherwise redo the block doing the risky multiplies in double,
  `(float)((double)a*b)`, which is one correctly rounded multiply (division: double
  rounding is innocuous since 53 >= 2*24+2). Measure the ceiling first by setting
  FTZ/DAZ in a throwaway build.
- **Guard logic must only decide speed.** With the underflow-flag check and exact
  fallbacks, a wrong guard can only cost time; the harness then needs a mutation test
  (break the exact path by one ulp) to prove the slow path is actually exercised.
- **Memory latency, not arithmetic, after vectorizing.** An update ran ~4x slower
  inside MrBayes than in the harness; prefetching and hugepages did not help, and
  splitting each call between two threads gained only on alignments of 768+ patterns
  (+35% at 1191, ~0 at 400). Two *independent* MrBayes processes, by contrast, both
  ran at full speed, so the second core went to whole chains instead.
- **Chains in parallel, one random stream, same run.** Keep every random draw on
  the main thread in the original order: a chain's move and its acceptance draw
  happen together (nothing in between draws or aborts in a BEAGLE run), and a swap
  draws exactly one number whatever its outcome, so its place can be reserved and
  the swap done when its run is ready. Only the likelihood runs elsewhere. Working
  run by run (run 1's acceptances, swaps and next moves while run 2's likelihoods
  compute) took two threads from 1.4x to 1.6x of one.
- **Look for stale reads before splitting shared scratch.** Per-chain scratch slots
  passed a 20-case smoke test and failed a quarter of the 88-case suite in the last
  digit: `ResetScalersPartition` leaves some scaler-node flags as it finds them, so
  which chain flipped the shared array last matters. That array stays shared and
  only the main thread flips it, in chain order (rare: numerical rescaling).
- **Copying a struct for a worker copies its inline arrays too.** A result written
  to `m->lnLike[...]` in a private ModelInfo copy never reached the real one.
- **ThreadSanitizer works here only with ASLR off** (`setarch x86_64 -R`, also around
  `configure`); otherwise "unexpected memory mapping". It found the one real race.
- **A second thread can lose.** With one busy process beside it, two coupled threads
  were ~15% slower than one (each generation waits for its slowest chain). Run-queue
  wait time did not separate the cases (14% quiet, 45% busy, depends on neighbours'
  priority); measuring generations per second each way and using the faster did.
  Per-chain scratch also costs ~4% in cache footprint, so the one-thread mode passes
  a single scratch set from chain to chain as MrBayes does.
- **Exact parsimony sums:** the parsimony-biased moves add float site weights into a
  double; when all weights are whole numbers below 2^24 any summation order is exact,
  so integer SIMD sums are fine -- checked at run time, original loop otherwise.
- MrBayes comparer: strip `[ID: n]`, `MCMC stamp`, the clock seed for default start
  values, progress-line times and the version banners, nothing else.
- **Benchmark only on a quiet host, and check.** Production jobs arrive mid-run; a
  timing wrapper that takes the lock, waits until `/proc/stat` shows the rest of the
  host idle, and repeats any run during which others used more than a quarter CPU
  (`scratchpad tools/qbench.sh` in that session) kept the numbers clean.

## 7. Delivery

1. Commit on a local branch of the tool's repo in the scratchpad, message describing
   each change, its exactness argument and the measurement.
2. `ops/<tool>/`: patch, a root install script that builds into a **new** `/opt` prefix and
   self-tests identity against the installed build, and a README with numbers and
   rollback. Mirror `ops/mafft/`.
3. The user installs as root; switch with the `*_BINARY` env var in
   `/etc/dikarya/dikarya.environment.live` (comment out the old line, do not add a
   duplicate); restart workers with the guarded wrapper after checking the queues
   (`AGENTS.md`, "Restarting Dikarya services"); confirm the new version string in a real
   job's log. Rollback is swapping the line back.
4. If the user wants it upstream: fork on their GitHub, README section with hardware,
   results, verification, credits (original authors; the session's model as co-author).
