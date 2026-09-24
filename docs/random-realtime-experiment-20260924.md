# 纯随机实时基线：Lv7 十局实验

日期：2026-09-24。十局全部完成并复核：**0 胜、10 负、0 局到时限，样本胜率 0%**。十个原始种子各运行一次，没有异常尝试、重赛或结果替换。

## 实验条件

- Altitude LE，Protoss 对 Zerg，API 难度 VeryHard / Lv7，RandomBuild，双方 handicap 100。
- 游戏种子 1–10；动作选择使用独立的 random.Random(seed)，策略种子与游戏种子相同。
- `realtime=True`，决策间隔至少 1 墙钟秒，每局上限 1200 游戏秒，最多四局并发。
- Astra 与 JEV 模型均关闭，模型 API 调用和 API 费用均为 0。
- 从当前执行器候选中均匀随机选择，包含 WAIT；保留基础合法性、策略上限、人口预测、工人分配、侦察和军队姿态执行。没有 Astra 计划过滤，也没有启用 advisory 的军队防抖。

“纯随机”指模型决策层被随机选择器替换，基础执行规则仍保留；不是每次都从全部 73 个动作中无条件抽取。随机策略不加入模拟的模型推理或网络延迟。只有多个候选时才发起选择请求。

## 逐局结果

| 种子 | 结果 | 游戏时间 | 随机决策数 | 回放与日志核验 |
|---:|---|---:|---:|---|
| 1 | 负 | 08:27 | 315 | 通过 |
| 2 | 负 | 11:20 | 461 | 通过 |
| 3 | 负 | 11:43 | 430 | 通过 |
| 4 | 负 | 06:59 | 280 | 通过 |
| 5 | 负 | 14:07 | 562 | 通过 |
| 6 | 负 | 12:01 | 502 | 通过 |
| 7 | 负 | 07:12 | 141 | 通过 |
| 8 | 负 | 14:11 | 573 | 通过 |
| 9 | 负 | 10:52 | 457 | 通过 |
| 10 | 负 | 06:56 | 183 | 通过 |

样本胜率的 Wilson 95% 区间为 0.0%–27.8%。这是固定十种子的开发样本，不能把样本胜率当作稳定胜率。

## 实时运行与零模型调用的证据

- 十局各有独立回放，每局 22 项核验通过：地图、种族、难度、handicap、胜负与终局帧均与原始配置及日志一致。
- 冻结源码 168 个文件的 SHA256 全部一致；十份回放哈希互不重复，原始事件日志与回放哈希二次复核一致。
- 共 3904 次随机决策，按各自种子完整重放随机序列，所有选项合法且概率均匀；Astra 请求、接受计划和军队防抖事件均为 0。
- 已记录决策请求的墙钟间隔最短 1.000 秒，中位数 1.109 秒；平均候选数 3.43。
- 各局心跳窗口内，游戏时间增量 / 墙钟时间增量为 0.9821–0.9980；结合运行参数与当前 observation 轮询实现，确认本批为实时运行。
- 开跑前，随机策略、终局观察和运行时相关的 19 项离线测试通过。

## 与旧加速基线的关系

| 批次 | 实时 | 决策时间基准 | 上限 | 胜 / 负 / 到时限 |
|---|---|---|---:|---|
| 2026-09-23 旧纯随机 | 否 | 游戏秒 | 1800 游戏秒 | 0 / 10 / 0 |
| 2026-09-24 本次纯随机 | 是 | 墙钟秒 | 1200 游戏秒 | 0 / 10 / 0 |

两批十局的实测胜率相同；这支持纯随机基线在本次实时设置下仍无胜局，不能据此证明加速与实时模式在所有策略和状态下等价。

随机选择器、基础机器人、动作接口、策略规则、宏观执行与导航的源码哈希相同；调度、启动、层级机器人和终局处理等文件存在版本差异。旧批次的十局也都在 1200 游戏秒前结束，因此较长的旧时限没有决定其胜负。具体协议与文件差异见派生分析中的 accelerated-comparison.json。

本次补齐了纯随机条件的实时证据，现已纳入[论文宏观主表](macro-configurations.md)。历史模型批次仍有版本与时序差异，不能将整张对照表称为完全同版本的因果消融。旧加速批次仍单独保留，未改写原始结果。

## 数据位置

```text
原始批次：
<research-archive>/Large-Language-Models-play-StarCraftII/jev_runs/random-macro-realtime-lv7-20260924-10-seeds

派生分析：
<research-archive>/paper/analysis/random-macro-realtime-lv7-20260924
  report.md / summary.json / games.csv / verification.json / accelerated-comparison.json

冻结源与复核脚本：
<research-archive>/.tools/random-macro-realtime-lv7-20260924
  registration.json / frozen-source.json / source / finalize.py / write_report.py
```

原始批次保留 protocol.json、results.json、results.csv、final-verification.json，以及每局的 run.json、summary.json、events.jsonl、game.SC2Replay 和 random-audit.json。本报告及 CSV 为派生文件，原始事件与回放未改写。

公开[逐局结果](../paper/data/macro_realtime/games.csv)与[证据清单](../paper/data/macro_realtime/manifest.json)保留本批配置和原始文件哈希；上述完整研究档案未随 Git 发布。
