---
name: tabular-ml
description: Reliable baselines and evaluation for CSV/tabular prediction tasks with pandas and scikit-learn, with pure-Python fallbacks.
---
# Tabular ML playbook

```python
import json, random
random.seed(0)
try:
    import numpy as np, pandas as pd
    from sklearn.model_selection import train_test_split, cross_val_score
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    from sklearn.linear_model import LogisticRegression
    from sklearn.ensemble import RandomForestClassifier, HistGradientBoostingClassifier
    from sklearn.dummy import DummyClassifier
    HAVE_SK = True
except ImportError:
    HAVE_SK = False
```

- **Inspect first:** shape, dtypes, missing-value counts, target balance. Print them.
- **Target leakage:** drop IDs, timestamps that encode the label, and any column computed from
  the target. Be suspicious of any single feature with near-perfect accuracy.
- **Model ladder:** `DummyClassifier` → linear model in a `make_pipeline(StandardScaler(), …)`
  → `HistGradientBoosting*`. Report all three.
- **Evaluation:** stratified split (`stratify=y`) for classification; 5-fold `cross_val_score`
  on the training split for model selection; touch the test split exactly once.
- **Metrics:** accuracy *and* a class-balance-aware metric (macro-F1 or ROC-AUC) for
  classification; RMSE and MAE for regression.
- **Without scikit-learn:** implement a majority baseline and a simple logistic regression or
  k-NN in pure Python; say in stdout which path ran.
- **Figures:** confusion matrix or residual plot, plus a bar chart of model scores
  (`matplotlib.use("Agg")`).
