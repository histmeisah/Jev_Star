# JEV-Star paper

**JEV-Star: Fast, Low-Cost StarCraft II Control with Language-Model Planning**

[Current PDF](JEV-Star.pdf) · [LaTeX source](main.tex) · [References](references.bib) · [Abstract](abstract.txt)

This is the existing NeurIPS-format research draft, not a record of a new
submission. The public repository retains one current PDF, the assets needed
to compile it, and the fixed tables used in its analysis. Author metadata and
any final submission remain the authors' responsibility.

The paper compares the initial JEV-only micro batch with the P0 JEV + GPT-6
batch: 35 maps and 105 episodes per method, 210 episodes in total. Macro
selection consists of the initial single-model run, four hierarchical wins
and one pressure-test loss. See the [experiment boundaries](../docs/experiments.md)
for intermediate versions and incomplete trials that are not pooled into the
paper's comparison.

## Data

- [Per-episode micro results](data/study/micro_episodes.csv)
- [Per-game cost estimates](data/study/game_costs.csv)
- [Method summaries](data/study/method_summary.csv)
- [Selected runs](data/study/runs.json)
- [Response latency and usage records](data/study/responses.csv)
- [Macro action records](data/study/macro_actions.csv)
- [Macro plans](data/study/macro_plans.csv)
- [Macro trajectories](data/study/macro_trajectories.csv)

These are fixed derived tables. Full chronological events, source replays and
videos remain in the original local research archive; the short paper listings
do not replace those logs. Paths in the included derived snapshots have been
made relative to the corresponding `macro/` or `micro/` module. Source hashes
still identify the original archived files.

Cost figures in the paper are estimates from known usage under its recorded
price assumptions, including an API-equivalent estimate for GPT-6. They are not
account invoices, and missing usage is not assumed free.

## Compile

The original `neurips_2026.sty` and its notices are retained. With a TeX
distribution installed, run in this directory:

```text
pdflatex main.tex
bibtex main
pdflatex main.tex
pdflatex main.tex
```

The seven vector PDF figures and two listings needed by `main.tex` are included.
Old drafts, build directories, duplicate image formats, bundled TeX binaries and
large MP4 files were excluded from the clean repository.
