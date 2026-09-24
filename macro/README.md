# Macro control / 宏观控制

[English](#english) | [简体中文](#简体中文)

## English

The macro controller plays complete Protoss games through BurnySC2. Its 73-command interface supports **pure random, JEV-only, Astra constrained + random, Astra constrained + JEV, and Astra advisory + JEV**. The consolidated release uses the final Astra implementation.

[Five configurations, results, and runnable commands](../docs/macro-configurations.md)

| File | Responsibility |
|---|---|
| [run_jev.py](sc2_rl_agent/starcraftenv_test/run_jev.py) | Policy/planner configuration and game lifecycle |
| [jev_agent.py](sc2_rl_agent/starcraftenv_test/agent/jev_agent.py) | JEV client, asynchronous scheduling, response validation |
| [random_macro.py](sc2_rl_agent/starcraftenv_test/agent/random_macro.py) | Seeded uniform choice, including WAIT |
| [astra_planner.py](sc2_rl_agent/starcraftenv_test/agent/astra_planner.py) | Persistent plans, validation, periodic/event triggers |
| [strategic_policy.py](sc2_rl_agent/starcraftenv_test/agent/strategic_policy.py) | Plan goals, budgets, and candidate rules |
| [macro_execution.py](sc2_rl_agent/starcraftenv_test/env/bot/macro_execution.py) | Production, research, placement, order feedback |
| [macro_navigation.py](sc2_rl_agent/starcraftenv_test/env/bot/macro_navigation.py) | Scouting and army execution |
| [run_random_ablation.py](sc2_rl_agent/starcraftenv_test/run_random_ablation.py) | Pure-random batch and replay/sequence audit |

Run `python jev_star.py macro ...` from the root after [setup](../README.md#quick-start). Combine `--policy jev|random` with `--planner none|codex`; with Astra, choose `--plan-mode constrained|advisory`. Model defaults are `jev-1.13.0` and `gpt-6-astra` / medium.

Constrained plans affect candidate filtering and execution checks. Advisory plans provide context without plan-dependent masks or mandatory army orders. Both retain executor resource, technology, production, availability, and command-lifecycle rules. “Astra does not constrain actions” does not mean all commands are always available.

Action IDs: 0–18 units/Archon; 19–33 buildings; 34–59 research; 60–63 scouting; 64 attack, 65 retreat, 72 defend; 66–70 Chronoboost; 71 WAIT. Astra does not emit per-unit orders. Worker allocation and continuing army orders run locally.

Games are realtime and asynchronous: SC2 advances during JEV and Astra requests. Decision starts are at least one wall-clock second apart. Astra has a nominal 60-game-second interval, event triggers, and a 180-game-second plan lifetime. The pure-random runner also supports separate accelerated experiments; the published Lv7 comparison uses realtime throughout.

Outputs default to `macro/jev_runs/<timestamp>/`: configuration and model-layer flags in `run.json`, chronological complete requests/responses in `events.jsonl`, planner prompts/replies, source fingerprints, summaries, replays, and an offline report. Permanent configuration/billing errors remain technical failures.

Results: pure random 0/10; Astra constrained + random 0/10 (nine verified replays and one human-adjudicated loss); Astra constrained + JEV 9/10; Astra advisory + JEV 3/10 with four time limits. No matched Lv7 JEV-only batch exists. See [experiment boundaries](../docs/experiments.md).

## 简体中文

宏观控制器通过 BurnySC2 运行完整 Protoss 对局。73 项动作接口支持 **纯随机、纯 JEV、Astra 约束＋随机、Astra 约束＋JEV、Astra 建议＋JEV**，当前发布采用最终 Astra 实现。

[五种配置、成绩和运行命令](../docs/macro-configurations.md)

安装后从根目录运行 `python jev_star.py macro ...`。使用 `--policy jev|random`、`--planner none|codex` 组合选择与规划；带 Astra 时使用 `--plan-mode constrained|advisory`。默认模型是 `jev-1.13.0` 与 `gpt-6-astra` / medium。

约束模式让计划参与预算、目标和姿态等候选筛选及执行复核。建议模式只提供上下文，不按计划裁剪动作或强制军队命令。执行器资源、科技、产能、可用性与命令生命周期规则保留；“Astra 不限制”不等于全部命令始终可用。

动作 ID：0–18 单位及 Archon，19–33 建筑，34–59 研究，60–63 侦察，64 进攻、65 撤退、72 防守，66–70 Chronoboost，71 WAIT。Astra 不直接生成逐单位命令，工人分配与已有军队命令持续执行。

实时对局在 JEV/Astra 推理期间继续推进。决策间隔至少 1 墙钟秒；Astra 默认 60 游戏秒定时规划，也可由事件触发，计划寿命 180 游戏秒。纯随机支持单独的非实时加速运行，但论文 Lv7 主表全部使用实时批次。

日志、完整请求与回复、规划提示、源码哈希、结果、回放和离线报告保存在 `macro/jev_runs/`。永久配置或计费错误保留技术失败分类。

Lv7 样本：纯随机 0/10；Astra 约束＋随机 0/10（9 局回放验证、1 局人工确认败北）；Astra 约束＋JEV 9/10；Astra 建议＋JEV 3/10，另有 4 局到时限。纯 JEV 暂无同条件 Lv7 批次。详见[实验记录](../docs/experiments.md)。
