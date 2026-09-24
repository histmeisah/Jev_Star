# JEV-Star paper / JEV-Star 论文

**JEV-Star: Fast, Low-Cost StarCraft II Control with Language-Model Planning**

[Read online / 在线阅读](PAPER.md) · [PDF](JEV-Star.pdf) · [LaTeX](main.tex) · [Abstract / 摘要](abstract.txt) · [Version and hashes / 版本与哈希](pdf-version.json)

## English

The September 24, 2026 edition presents five macro configurations: pure random, JEV-only, Astra constrained + random, Astra constrained + JEV, and Astra advisory + JEV. Four have ten-seed realtime Lv7 batches: pure random 0/10, constrained random 0/10, constrained JEV 9/10, and advisory JEV 3/10 wins, with four time limits in advisory JEV. JEV-only has no matched Lv7 evaluation. Constrained random includes one human-adjudicated loss without a replay; 39 of the 40 selected attempts have verified replays.

The final Astra implementation is described by its role in each configuration. Realtime evidence comes from advancing game frames during all 15,505 returned JEV requests and 344 returned Astra requests in the two JEV-planner batches. Archived source and transport differences remain explicit; this is not a fully controlled five-way causal ablation.

The 210-episode micro comparison and historical macro replay/build-order/cost analyses remain separate. Historical USD figures are not attributed to the new batches. Micro uses fixed stepping; the realtime claim concerns full-game macro control.

This PDF is rebuilt from the included LaTeX and assets. The online reader renders this same PDF. Historical figures and sections have been synchronized from the research manuscript before integrating the new macro evaluation.

## 简体中文

2026 年 9 月 24 日版按五种配置组织宏观方法：纯随机、纯 JEV、Astra 约束＋随机、Astra 约束＋JEV、Astra 建议＋JEV。除纯 JEV 外，其余四组已有实时 Lv7 十局，胜局分别为 0、0、9、3；建议模式另有 4 局到时限。纯 JEV 同条件成绩留空。Astra＋随机含 1 局人工确认败北，40 次选定对局共 39 份已验证回放。

Astra 作为最终系统实现介绍。两组 Astra＋JEV 的 15,505 次 JEV 回复与 344 次 Astra 回复均记录到推理期间游戏帧推进。历史源码及服务传递差异在证据清单中保留，不宣称严格五组因果消融。

微观仍保留 210 局比较；宏观旧版回放、建造序列和成本作为历史案例，不把旧成本套用到新批次。微观是固定步进，实时结论仅适用于完整宏观对局。

PDF、LaTeX、图表与在线阅读页同步更新。

## Data / 数据

- [Five configurations / 五种配置](../docs/macro-configurations.md)
- [40 macro attempts / 宏观逐局结果](data/macro_realtime/games.csv)
- [Configuration summary / 配置汇总](data/macro_realtime/configurations.csv)
- [Protocols, source fingerprints, replay and event hashes / 证据清单](data/macro_realtime/manifest.json)
- [210 micro episodes / 微观逐局结果](data/study/micro_episodes.csv)
- [Historical run selection / 历史案例选择](data/study/runs.json)
- [Historical latency and usage / 历史响应记录](data/study/responses.csv)
- [Historical model-cost estimates / 历史成本估算](data/study/game_costs.csv)

Complete chronological events and source replays remain in the local research archive. Derived tables do not replace raw logs. Credentials, provider configuration and personal paths are excluded from the published evidence export.

## Build / 构建

With a TeX distribution, run inside this directory:

```text
pdflatex main.tex
bibtex main
pdflatex main.tex
pdflatex main.tex
```

Or use Tectonic with the included style and assets. Optional figure/rendering dependencies are in requirements.txt. From the repository root:

```powershell
python -m pip install -r paper/requirements.txt
python scripts/build_macro_figures.py
python scripts/render_paper.py --pdf paper/main.pdf --date 2026-09-24
```

Regenerate the primary macro tables from the original archive without launching games:

```powershell
python scripts/export_macro_evaluation.py --runs-root <archive>/jev_runs
```

The exporter checks recorded event and replay hashes before writing tables. The original NeurIPS style and notices are retained; this is a preprint.
