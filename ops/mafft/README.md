# MAFFT build used by Dikarya

Production MAFFT is not the Ubuntu package. Since 2026-10-07 the workers run an
exact-speedup build of MAFFT 7.526, selected by `MAFFT_BINARY` in
`/etc/dikarya/dikarya.environment.live`:

| build | prefix | what it is |
|---|---|---|
| `v7.526-opt5` | `/opt/mafft-7.526-opt5` | Josh Walker's fork, https://github.com/joshuaowalker/mafft at `7f1af7b` |
| `v7.526-opt5-dikarya1` | `/opt/mafft-7.526-opt5-dikarya1` | opt5 plus `0001-mafft-dikarya1-avx2.patch` (this directory) |
| `v7.526-opt8` | `/opt/mafft-7.526-opt8` | the same fork at `322527b`, which already contains dikarya1; built by `install-mafft-opt8.sh` |

opt8 is the newest and fastest of these; see [opt8](#opt8-2026-10-13) for the measurements
and how to switch. Which one is live is whatever `MAFFT_BINARY` names (new jobs'
`logs/alignment.log` print the version).

All of them promise the same thing: for the same input and options, the alignment is
**byte-identical to stock MAFFT** built with gcc and run with `--thread 1`. Only the
speed changes. `--thread 2` (what the main worker uses) is not reproducible run to run
in any MAFFT build, stock included.

Upstream check (2026-10-07): the newest official source is GitLab `sysimm/mafft`
`main` at `0a2319b` (7.526 plus commits through December 2025); the download page
offers 7.525. opt5 is built directly on `0a2319b`, so nothing newer exists to rebase on.

## What dikarya1 adds

opt5's x86 kernels need AVX-512. This host is an Intel Xeon E5-2690 v4 (Broadwell,
AVX2 + FMA, no AVX-512), so opt5 ran its SSE4.1 and scalar fallbacks here. The patch adds
AVX2 paths, each bit-exact with the code it replaces (gcc never contracts `a*b+c` under
the Makefile's `-std=c99`, and the vector code uses a separate multiply and add):

- **`Lfill_int`** (the L-INS-i all-pairs local alignment, ~60% of an L-INS-i run): 8 cells
  per step. Gap cells store a marker instead of the gap offset, all DP rows are kept,
  and `Ltracking` recomputes the offset (integer arithmetic, so exact) for the few
  marked cells on the traceback path. 1.68 -> 0.82 ns per DP cell.
- **`scarr_fill()`**: the 26x26 profile-score loop inside every `match_calc`, skipping the
  zero terms (exact: the sum starts at +0 and is never -0). This was the bulk of
  `A__align`, i.e. of FFT-NS-i.
- **`Falign`**: its per-call working rows are `malloc`ed instead of `calloc`ed; they are
  only used as strings that are written before they are read. Clearing ~6 MB per call
  was a quarter of an FFT-NS-i run.
- AVX2 ports of opt5's AVX-512-only `A_row` / `partA_row` fills, and an 8-wide row-max search.

The full write-up, including what was tried and did not help, is in the commit message
of the patch and in the fork README.

## Measured on this host (14 real Dikarya inputs, `--adjustdirection --auto`)

| | `--thread 2` total | vs. Ubuntu 7.505 |
|---|---|---|
| Ubuntu MAFFT 7.505 | ~367 s | 1.0x |
| opt5 | 199 s | 1.8x |
| opt5-dikarya1 | 99 s | 3.7x |

Full L-INS-i (`--localpair --maxiterate 1000`) is 1.7-1.9x faster than opt5.

Verification before this patch was adopted: byte-identical to Ubuntu 7.505 at
`--thread 1` on 52 real inputs (L-INS-i and FFT-NS-i), both as built and with
`-DMAFFT_POISON_TEST` (fills every buffer the patch stopped clearing with junk).

## opt8 (2026-10-13)

`v7.526-opt8` is Josh Walker's newest version of the fork. Its history includes dikarya1
(the tag `v7.526-opt5-dikarya1` is on the same branch), so the AVX2 paths above are part of
it, merged into one feature-test header (`core/mafft_simd.h`) shared by every target. What
it adds for this CPU are the newer portable changes from opt6-opt8, mostly in the
bookkeeping passes and the refinement; the fork's README describes them.

Measured on this host against dikarya1, 43 real job inputs (6-827 sequences, up to 3.3 kb),
both builds run alternately under the benchmark lock and repeated when another process
used the CPU:

| mode | inputs | `--thread 1` | `--thread 2` |
|---|---|---|---|
| L-INS-i (`--adjustdirection --localpair --maxiterate 1000`) | 35 | 1.12x | 1.15x |
| `--adjustdirection --auto` | 43 | 1.12x | 1.13x |
| FFT-NS-2 (`--retree 2 --maxiterate 2`) | 6 | 1.10x | |
| G-INS-i (`--globalpair --maxiterate 1000`) | 5 | 1.00x | |
| direction pre-pass (`--adjustdirectionaccurately --retree 1 --maxiterate 0`) | 8 | 1.00x | 1.03x |
| `--adjustdirectionaccurately --auto` | 4 | 1.02x | |

By stage: the refinement (`dvtditr`) is 1.16x (L-INS-i) to 1.23x (FFT-NS-i) faster, and
`tbfast`, which runs the all-pairs local alignment and the progressive step, 1.10x. Every
`--thread 2` input that first measured below 1.0x came out at 1.0x or faster in four
repeats each; two-thread runs vary by up to 40% on their own. Neither build speeds up the
accurate direction check (`makedirectionlist`): it takes 27-75 s on 130-250 sequences,
against ~5 s for the whole `--auto` alignment of the same input.

Verification: byte-identical to dikarya1 on all 101 `--thread 1` runs above (all six
modes), and so to stock MAFFT, which dikarya1 was checked against.

### Installing opt8 / switching / rolling back

As root on the production host (the build is `-march=native`, so build where it runs):

```zsh
hostname                                   # must print mycolab
bash /var/www/dikarya/ops/mafft/install-mafft-opt8.sh
```

It clones the fork at the pinned commit, builds with the same flags as dikarya1, and
refuses to finish unless the new build reproduces the installed dikarya1 byte for byte in
15 runs: synthetic DNA families (some sequences reverse-complemented, one family over
1,000 bp so `--auto` takes FFT-NS-i) and MAFFT's own protein sample, in every mode Dikarya
runs. Then point Dikarya at it (comment out the old line rather than adding a second one),
and have an agent restart the workers with the guarded wrapper:

```zsh
sed -i 's|^MAFFT_BINARY=/opt/mafft-7.526-opt5-dikarya1/bin/mafft$|# & (replaced by opt8)|' /etc/dikarya/dikarya.environment.live
echo 'MAFFT_BINARY=/opt/mafft-7.526-opt8/bin/mafft' >> /etc/dikarya/dikarya.environment.live
grep -n MAFFT /etc/dikarya/dikarya.environment.live
```

New jobs' `logs/alignment.log` then report `Version 7.526-opt8`. To roll back, swap the
two lines and restart the workers; the dikarya1 prefix is left in place.

## Installing dikarya1 (the rollback build)

As root on the production host (the build is `-march=native`, so build where it runs):

```zsh
hostname                                   # must print mycolab
bash /var/www/dikarya/ops/mafft/install-mafft-dikarya.sh
```

The script refuses to finish unless the new build reproduces the installed opt5
build byte-for-byte on a synthetic DNA family and MAFFT's own protein sample. Then point
Dikarya at it (comment out the old line rather than adding a second one), and have an
agent restart the workers with the guarded wrapper:

```zsh
sed -i 's|^MAFFT_BINARY=/opt/mafft-7.526-opt5/bin/mafft$|# & (replaced by dikarya1)|' /etc/dikarya/dikarya.environment.live
echo 'MAFFT_BINARY=/opt/mafft-7.526-opt5-dikarya1/bin/mafft' >> /etc/dikarya/dikarya.environment.live
grep -n MAFFT /etc/dikarya/dikarya.environment.live
```

New jobs' `logs/alignment.log` then report `Version 7.526-opt5-dikarya1`. To roll back,
swap the two lines and restart the workers; the opt5 prefix is left in place.

`MAFFT_DEFAULT_STRATEGY=linsi` (full L-INS-i for < 200 sequences, see `app/config.py`)
is a separate switch and is still off.
