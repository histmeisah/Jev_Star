# Five macro configurations / 五种宏观配置

The configurations separate three choices: whether Astra supplies a plan, whether it constrains candidates, and whether JEV or a local random selector chooses the action.

五种配置分别控制：是否使用 Astra 计划、计划是否限制候选、底层由 JEV 还是随机选择器决策。

| Configuration / 配置 | Astra | Plan filters / 计划筛选 | Selector / 底层选择 |
|---|---|---|---|
| Pure random / 纯随机 | None / 无 | None / 无 | Uniform random / 均匀随机 |
| JEV-only / 纯 JEV | None / 无 | None / 无 | JEV |
| Astra constrained + random / Astra 约束＋随机 | Persistent plan / 持续计划 | Enabled / 启用 | Uniform random / 均匀随机 |
| Astra constrained + JEV / Astra 约束＋JEV | Persistent plan / 持续计划 | Enabled / 启用 | JEV |
| Astra advisory + JEV / Astra 建议＋JEV | Persistent plan / 持续计划 | Disabled / 不启用 | JEV |

## How they work / 工作方式

**Pure random:** Neither model is called. A separate seeded RNG samples uniformly from current executor candidates, including WAIT. The observation is logged but does not rank alternatives. **纯随机：** 不调用模型；从当前候选中等概率选择，包含 WAIT，不根据状态排序。

**JEV-only:** JEV uses the structured state, recent execution feedback, and available actions without an Astra plan. **纯 JEV：** 根据结构化状态、近期反馈及候选选择动作，不生成或保留 Astra 计划。

**Astra constrained + random:** Astra supplies goals, budgets, production priorities, and army intent. The adapter applies plan rules; uniform random selection replaces JEV within the resulting set. Astra is still called. **Astra 约束＋随机：** 保留规划与候选筛选，在最终集合内随机选择。计划优先级文字不会让随机选择器获得语义判断能力。

**Astra constrained + JEV:** Plans affect candidate filtering and execution checks; JEV selects actions using state, the active plan, and filtered candidates. **Astra 约束＋JEV：** 计划参与筛选和执行复核，JEV 结合状态与计划选择动作。

**Astra advisory + JEV:** Astra provides context without plan-dependent budget, production, goal, or posture masks, mandatory army orders, or rejection solely because a plan changed. The executor retains configured availability and command-lifecycle rules. **Astra 建议＋JEV：** 计划作为上下文，不按计划裁剪动作或强制下一条军队命令，执行器自身规则保留。

“Unconstrained” means **not constrained by Astra's plan**, not that every action is always executable. All configurations retain placement, worker allocation, production, scouting, and army execution. Random selection is over current candidates, not all 73 IDs without prerequisites. The consolidated release uses the final Astra implementation; archived batch versions are recorded separately.

“不限制”指 **Astra 不限制底层动作空间**，不是全部 73 项动作始终可用。当前发布采用最终 Astra 实现，历史实验源码版本另行记录，不把代码整合当作旧批次在新版本重跑。

## Realtime Lv7 results / 实时 Lv7 结果

Altitude LE; Protoss versus Zerg VeryHard / Lv7; RandomBuild; handicap 100; seeds 1–10; realtime; minimum 1 wall-clock second between decisions; 1200-game-second limit.

| Configuration / 配置 | Wins / 胜 | Losses / 负 | Time limit / 到时限 | Sample win rate / 样本胜率 |
|---|---:|---:|---:|---:|
| Pure random / 纯随机 | 0 | 10 | 0 | 0% |
| JEV-only / 纯 JEV | 0 | 10 | 0 | 0% |
| Astra constrained + random / Astra 约束＋随机 | 0 | 10* | 0 | 0%* |
| Astra constrained + JEV / Astra 约束＋JEV | 9 | 1 | 0 | 90% |
| Astra advisory + JEV / Astra 建议＋JEV | 3 | 3 | 4 | 30% |

\* Nine replay-verified losses and one human-adjudicated loss (seed 5), whose terminal error prevented replay capture. There are **50 selected attempts and 49 verified replays**. JEV-only contributes ten replay-verified realtime Lv7 losses. Its historical Lv2 case and the entire four-attempt pilot with unsynchronized SDK IDs are excluded.

\* Astra＋随机组含 9 局回放验证败北和 1 局人工确认败北（种子 5），该局因终局错误未保存回放，原始技术核验仍为未确认。主表共 50 局、49 份已验证回放。纯 JEV 新增十局均由回放确认败北；早期 Lv2 案例及 SDK 编号未同步的整批四次试跑不计入主表。详见[补测报告](jev-only-realtime-experiment-20260924.md)。

Win rates include all ten attempts per evaluated configuration. Game-time limits differ from model-request timeouts. These fixed-seed development samples have version and service-timing differences and do not isolate a single causal effect.

各已评估配置以全部十局为分母，到游戏时限不计胜，也与模型请求超时分开。样本存在版本与服务时序差异，不是只改变一个变量的严格因果消融。

## Reproduce / 复现

Run from the repository root after [setup](../README.md#quick-start). These commands launch games; model-enabled configurations require the corresponding credentials.

完成[环境安装](../README.md#快速开始)后，从根目录运行。以下命令会启动游戏。

```powershell
# 1. Pure random / 纯随机
python jev_star.py macro --policy random --planner none --realtime --difficulty VeryHard --seed 1 --game-time-limit 1200

# 2. JEV-only / 纯 JEV
python jev_star.py macro --policy jev --planner none --realtime --difficulty VeryHard --seed 1 --game-time-limit 1200

# 3. Astra constrained + random / Astra 约束＋随机
python jev_star.py macro --policy random --planner codex --plan-mode constrained --planner-effort medium --realtime --difficulty VeryHard --seed 1 --game-time-limit 1200

# 4. Astra constrained + JEV / Astra 约束＋JEV
python jev_star.py macro --policy jev --planner codex --plan-mode constrained --planner-effort medium --realtime --difficulty VeryHard --seed 1 --game-time-limit 1200

# 5. Astra advisory + JEV / Astra 建议＋JEV
python jev_star.py macro --policy jev --planner codex --plan-mode advisory --planner-effort medium --realtime --difficulty VeryHard --seed 1 --game-time-limit 1200
```

Defaults: Altitude LE, Zerg, JEV policy, no planner. Random uses a separate RNG, defaulting to the game seed. Model-enabled games require realtime. Pure random needs no API key; Astra＋random needs Astra authentication but no JEV key.

默认地图 Altitude LE、对手 Zerg、策略 JEV、不带规划。随机策略使用独立 RNG。纯随机无需密钥；Astra＋随机只需要 Astra 登录。

Ten-seed pure-random batch with replay auditing / 带回放核验的纯随机十局：

```powershell
python jev_star.py macro-random-suite --output-dir jev_runs/random-realtime-lv7 --difficulty VeryHard --games 10 --start-seed 1 --realtime --game-time-limit 1200 --parallel 4
```

[Per-game results / 逐局结果](../paper/data/macro_realtime/games.csv) · [Summary / 配置汇总](../paper/data/macro_realtime/configurations.csv) · [Protocol and evidence / 协议与证据](../paper/data/macro_realtime/manifest.json) · [Export script / 数据导出脚本](../scripts/export_macro_evaluation.py)
