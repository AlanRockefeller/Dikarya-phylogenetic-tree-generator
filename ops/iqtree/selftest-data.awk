# Synthetic aligned DNA family for the IQ-TREE identity self-test. Deterministic
# (fixed srand seed): 80 sequences of 700 columns descended through a random
# tree, with site-rate variation, a few gap runs, ambiguity codes and three
# exact duplicates, so the duplicate-removal, gap and ambiguous-state paths run.
BEGIN {
    srand(20261007); split("ACGT", b, ""); split("RYKMSWN", amb, "")
    L = 700; N = 80
    for (i = 1; i <= L; i++) { anc = anc b[int(rand()*4)+1]; r[i] = rand() < 0.4 ? 0.0 : rand()^2 }
    seq[1] = anc
    for (s = 2; s <= N; s++) {
        p = seq[int(rand()*(s-1))+1]; out = ""
        for (i = 1; i <= L; i++) {
            c = substr(p, i, 1)
            if (c != "-" && rand() < 0.12 * r[i]) c = b[int(rand()*4)+1]
            out = out c
        }
        seq[s] = out
    }
    for (s = 1; s <= N; s++) {
        x = seq[s]
        if (s % 9 == 0) { g = int(rand()*(L-30)) + 1; x = substr(x, 1, g-1) sprintf("%25s", "") substr(x, g+25); gsub(/ /, "-", x) }
        if (s % 7 == 0) { a = int(rand()*L) + 1; x = substr(x, 1, a-1) amb[int(rand()*7)+1] substr(x, a+1) }
        seq[s] = x
    }
    seq[N-1] = seq[3]; seq[N-2] = seq[5]; seq[N-3] = seq[5]
    for (s = 1; s <= N; s++) printf(">taxon%03d\n%s\n", s, seq[s])
}
