---
name: experiment-design
description: Turn a hypothesis into a controlled, falsifiable experiment with baselines, ablations and a pre-declared metric.
---
# Experiment design

1. **State the claim as a comparison.** "Method A beats baseline B on metric M by at least δ on data D."
   If you cannot write that sentence, the hypothesis is not testable yet — narrow it.
2. **Fix the metric before running anything.** Write it into `results.json` keys up front
   (`metric_name`, `higher_is_better`). Never pick the metric after seeing results.
3. **Always include a trivial baseline** (majority class, mean predictor, random, or the
   simplest published method). A result without a baseline is uninterpretable.
4. **Hold out evaluation data first.** Split once with a fixed seed, before any feature work.
   Fit scalers/encoders on the training split only.
5. **Control randomness.** Seed `random`, `numpy` and any framework. Run ≥3 seeds when runtime
   allows and report mean ± std.
6. **One change at a time.** Each ablation removes or alters exactly one component.
7. **Budget the runtime.** Estimate per-run seconds on a small slice, then scale; stay well
   under the sandbox timeout.
8. **Record negative results.** A null result with a sound design is a finding; write it to
   `results.json` with the same care as a positive one.

Output checklist: `results.json` with every number, at least one PNG figure comparing methods,
stdout summary with the headline comparison.
