# Synthetic aligned DNA for the MrBayes identity self-test, as the NEXUS data
# block Dikarya writes. Deterministic (fixed srand seed): 96 sequences of 720
# columns descended through a random tree, divergent enough that partial
# likelihoods underflow into the subnormal range between MrBayes's scaler
# nodes (the exact slow paths of the patched kernels run), with invariant and
# fast sites, gap runs, partially ambiguous codes (those tips are partials, not
# states), runs of N, and two exact duplicates.
BEGIN {
    srand(20261009); split("ACGT", b, ""); split("RYKMSW", amb, "")
    L = 720; N = 96
    for (i = 1; i <= L; i++) { anc = anc b[int(rand()*4)+1]; r[i] = rand() < 0.35 ? 0.0 : 3 * rand()^2 }
    seq[1] = anc
    for (s = 2; s <= N; s++) {
        p = seq[int(rand()*(s-1))+1]; out = ""
        for (i = 1; i <= L; i++) {
            c = substr(p, i, 1)
            if (rand() < 0.16 * r[i]) c = b[int(rand()*4)+1]
            out = out c
        }
        seq[s] = out
    }
    for (s = 1; s <= N; s++) {
        x = seq[s]
        if (s % 8 == 0) { g = int(rand()*(L-40)) + 1; x = substr(x, 1, g-1) sprintf("%30s", "") substr(x, g+30); gsub(/ /, "-", x) }
        if (s % 11 == 0) { g = int(rand()*(L-20)) + 1; x = substr(x, 1, g-1) "NNNNNNNNNNNN" substr(x, g+12) }
        if (s % 6 == 0) for (k = 0; k < 3; k++) { a = int(rand()*L) + 1; x = substr(x, 1, a-1) amb[int(rand()*6)+1] substr(x, a+1) }
        seq[s] = x
    }
    seq[N-1] = seq[4]; seq[N-2] = seq[9]
    printf("#NEXUS\nbegin data;\ndimensions ntax=%d nchar=%d;\nformat datatype=dna missing=? gap=-;\nmatrix\n", N, L)
    for (s = 1; s <= N; s++) printf("SEQ%06d %s\n", s, seq[s])
    printf(";\nend;\n")
}
