---
name: statistical-analysis
description: Report uncertainty correctly — confidence intervals, paired tests, effect sizes, multiple comparisons.
---
# Statistical analysis

- **Report uncertainty with every number.** Mean ± std over seeds, or a 95% bootstrap CI
  (1,000+ resamples) for a single held-out set.
- **Paired comparisons:** when two methods are scored on the same items, use a paired test
  (paired t-test, Wilcoxon signed-rank, or McNemar for classification correctness).
- **Effect size over p-values.** Report the difference and its CI; a p-value alone says nothing
  about magnitude.
- **Multiple comparisons:** with k comparisons, apply Holm–Bonferroni and say so.
- **Small samples:** below ~30 items, prefer exact or permutation tests and say the result is
  preliminary.
- **Write it down:** every statistic goes into `results.json` (e.g. `"acc_diff": 0.031,
  "acc_diff_ci95": [0.004, 0.058], "p_wilcoxon": 0.02`).

Pure-Python bootstrap:

```python
import random, statistics
def bootstrap_ci(xs, n=2000, seed=0):
    rng = random.Random(seed)
    means = sorted(statistics.fmean(rng.choices(xs, k=len(xs))) for _ in range(n))
    return means[int(0.025 * n)], means[int(0.975 * n)]
```
