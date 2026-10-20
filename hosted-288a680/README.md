# Hosted CI and final merge evidence

PR head `288a680dd85dde9766fce3c4d8d962b42bd12203` and tested synthetic merge `2ccc55500471314c12631596c09bfda9335efc50` share tree `dbb18bcb152e97f7bc88964e3b33063a5858cc34`. The final main merge is identified in `summary.json`. Five complete successful Python jobs are bound to this final tree. Final source, installed-command, Slurm, Bash, and umask receipts retain this hosted identity.

The first matrix at `954213c` and second workflow at `ffda87d` failed. All five raw job logs and exact outcomes from each attempt remain historical. Test-fixture, readiness, and experimental build-isolation corrections retain their own evidence. The exact historical helper failure cause and remote-log wall-delay attribution remain unproved. These earlier attempts are not final acceptance. Qualification units remain separate; overlapping counts are not additive.

Slurm command recordings preserve exact raw stdout, stderr, and owned daemon metadata. Compiler and Buildx caches are excluded. Complete final hosted Python job logs are losslessly compressed in `python/`. Explicit excerpts preserve exact source line numbers, original complete-log hashes, and job URLs; they remain subsets of those retained full logs. Earlier local full logs and JUnit results remain in v3.

No physical GPU, production SSH, multi-node, heterogeneous site, human usability, or scientific efficacy qualification is implied. This append does not modify the tested source tree.
