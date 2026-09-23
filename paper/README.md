# JEV-Star paper / JEV-Star 论文

[English](#english) | [简体中文](#简体中文)

**JEV-Star: Fast, Low-Cost StarCraft II Control with Language-Model Planning**

**JEV-Star：语言模型规划驱动的快速、低成本星际争霸 II 控制**

## English

[Read online](PAPER.md) ? [Current PDF](JEV-Star.pdf) · [LaTeX source](main.tex) · [References](references.bib) · [Abstract](abstract.txt)

The current PDF is the **26-page edition supplied on September 23, 2026**,
copied without changing its contents. [Version and SHA-256](pdf-version.json).
The LaTeX files and analysis assets retain the earlier repository snapshot;
recompiling that snapshot may differ from this supplied PDF.

The paper compares the initial JEV-only micro batch with the P0 JEV + GPT-6
batch: 35 maps and 105 episodes per method, 210 episodes in total. Macro
selection consists of the initial single-model run, four hierarchical wins
and one pressure-test loss. See the [experiment boundaries](../docs/experiments.md)
for intermediate versions and incomplete trials that are not pooled into the
paper's comparison.

### Data

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

### Compile

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

## 简体中文

[当前 PDF](JEV-Star.pdf) · [LaTeX 源码](main.tex) · [参考文献](references.bib) · [摘要](abstract.txt)

当前 PDF 为 **2026 年 9 月 23 日提供的 26 页版本**，原样收录，点击上方链接即可进入 GitHub 论文预览页。[版本与 SHA-256](pdf-version.json)。LaTeX 文件和分析资产仍为仓库先前的源码快照，重新编译的结果可能与这份 PDF 不同。

论文比较初始纯 JEV 微操批次与 P0 JEV＋GPT-6 批次：每种方法覆盖 35 张地图、105 局，两种方法合计 210 局。宏观样本包括初始单模型对局、四局分层控制胜利和一局压力测试失利。未合并进论文比较的中间版本和不完整试验，见 [实验与版本边界](../docs/experiments.md)。

### 数据

- [微观逐局结果](data/study/micro_episodes.csv)
- [每局成本估算](data/study/game_costs.csv)
- [方法汇总](data/study/method_summary.csv)
- [选定运行记录](data/study/runs.json)
- [响应延迟与用量记录](data/study/responses.csv)
- [宏观动作记录](data/study/macro_actions.csv)
- [宏观计划](data/study/macro_plans.csv)
- [宏观对局轨迹](data/study/macro_trajectories.csv)

这些是固定的派生统计表。完整的时间顺序事件、源 replay 和视频保留在原本地研究档案中；论文中的简短日志引用不替代原始日志。随仓库提供的派生快照已将路径转换为对应 `macro/` 或 `micro/` 模块的相对路径，源文件哈希仍用于识别原始归档文件。

论文成本来自已知用量及文中记录的价格假设，其中包含 GPT-6 的 API 等价成本估算。这些不是账户账单，缺失用量也不按免费计算。

### 编译

原始 `neurips_2026.sty` 及其声明已保留。安装 TeX 发行版后，在本目录运行：

```text
pdflatex main.tex
bibtex main
pdflatex main.tex
pdflatex main.tex
```

仓库包含 `main.tex` 所需的七张矢量 PDF 图和两份日志引用。旧草稿、构建目录、重复图片格式、打包的 TeX 程序和大型 MP4 文件已从整理后的仓库中排除。
