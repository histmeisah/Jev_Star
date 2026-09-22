# Macro control / 宏观控制

[English](#english) | [简体中文](#简体中文)

## English

The current version, `macro-v2.2.1`, controls full Protoss games. It retains the LLM Play SC2 Protoss executor and adds native JEV Choice requests, asynchronous Astra phase planning, action precondition checks, order lifecycle management, and navigation on top of BurnySC2.

| Entry point / file | Responsibility |
| --- | --- |
| [run_jev.py](sc2_rl_agent/starcraftenv_test/run_jev.py) | Real-time game and model lifecycles; output directories |
| [jev_agent.py](sc2_rl_agent/starcraftenv_test/agent/jev_agent.py) | JEV HTTP client, timeouts, stale results, and termination on permanent errors |
| [astra_planner.py](sc2_rl_agent/starcraftenv_test/agent/astra_planner.py) | Codex invocation, plan validation, and periodic or event-based triggers |
| [macro_contract.py](sc2_rl_agent/starcraftenv_test/agent/macro_contract.py) | Shared constraints and implementation versions for 73 actions |
| [strategic_policy.py](sc2_rl_agent/starcraftenv_test/agent/strategic_policy.py) | Plan goals, budgets, and legal candidates |
| [macro_execution.py](sc2_rl_agent/starcraftenv_test/env/bot/macro_execution.py) | Production, research, abilities, and order feedback |
| [macro_navigation.py](sc2_rl_agent/starcraftenv_test/env/bot/macro_navigation.py) | Scouting and persistent army stances |

Run `python jev_star.py macro ...` from the repository root; see the [main README](../README.md#english) for setup. By default, the controller starts at most one JEV request per second. Games use `realtime=True` and continue advancing while the model is processing. JEV results are checked against the latest state and plan version; stale results are discarded. Planning runs in the background.

Action IDs: 0–18 cover unit production and Archon merging; 19–33 buildings; 34–59 research; 60–63 scouting; 64 attack, 65 retreat, and 72 defend; 66–70 Chronoboost; 71 wait. The planner does not directly emit per-unit commands. Micro skills are limited to the executor's implemented capabilities; writing an instruction in a plan does not add a new executable skill.

Use `--planner none` to run without planning. With planning enabled, the default Astra model is `gpt-6-astra`; you can explicitly set `--planner-effort medium`. The default planning interval is 60 game seconds, with earlier triggers for urgent events. Use `--help` to inspect model timeouts, minimum intervals, plan lifetimes, and request limits.

Logs default to `macro/jev_runs/<timestamp>/`, with an automatically generated offline `report.html`. Permanent configuration or billing errors, such as HTTP 402, stop the run. Original records retain the failure classification instead of reporting an ordinary game result. See [experiments](../docs/experiments.md) and [logging](../docs/logs-and-replays.md).

This directory excludes legacy Gym registration, chat models, retrieval memory, and script bots unrelated to JEV. The retained `Protoss_Bot` base class and JEV behavior implementations have passed the existing regression tests.

## 简体中文

当前为 `macro-v2.2.1`，控制 Protoss 完整对局。代码保留 LLM Play SC2 的 Protoss 执行器，在 BurnySC2 上增加原生 JEV Choice、异步 Astra 阶段规划、动作条件检查、订单生命周期及导航。

| 入口 / 文件 | 职责 |
| --- | --- |
| [run_jev.py](sc2_rl_agent/starcraftenv_test/run_jev.py) | 实时游戏与模型生命周期、输出目录 |
| [jev_agent.py](sc2_rl_agent/starcraftenv_test/agent/jev_agent.py) | JEV HTTP 客户端、超时、过期结果、永久错误停止 |
| [astra_planner.py](sc2_rl_agent/starcraftenv_test/agent/astra_planner.py) | Codex 调用、计划校验、定时及事件触发 |
| [macro_contract.py](sc2_rl_agent/starcraftenv_test/agent/macro_contract.py) | 73 个动作的共同约束与实现版本 |
| [strategic_policy.py](sc2_rl_agent/starcraftenv_test/agent/strategic_policy.py) | 计划目标、预算及合法候选 |
| [macro_execution.py](sc2_rl_agent/starcraftenv_test/env/bot/macro_execution.py) | 生产、研究、能力、订单反馈 |
| [macro_navigation.py](sc2_rl_agent/starcraftenv_test/env/bot/macro_navigation.py) | 侦察与持续军队姿态 |

从仓库根目录运行 `python jev_star.py macro ...`；安装方法见 [总说明](../README.md#简体中文)。默认每秒最多发起一次 JEV 请求，游戏 `realtime=True`，模型等待期间继续推进。JEV 结果按最新状态和计划版本复核，过期结果丢弃；计划调用在后台执行。

动作 ID：0–18 单位生产及 Archon，19–33 建筑，34–59 研究，60–63 侦察，64 进攻、65 撤退、72 防守，66–70 Chronoboost，71 等待。规划模型不直接输出逐单位命令。实际微操技能只覆盖已实现的执行能力，不能把文字计划当作额外技能接口。

不带规划时使用 `--planner none`。带规划时默认 Astra 为 `gpt-6-astra`，命令中可显式使用 `--planner-effort medium`。默认周期 60 游戏秒，紧急事件可提前触发；模型时限、最小间隔、计划寿命和请求上限均可通过 `--help` 查看。

日志默认写入 `macro/jev_runs/<timestamp>/`，并自动生成离线 `report.html`。HTTP 402 等永久配置/计费错误停止该运行；原始记录保留失败分类，不伪装成正常对局结果。详见 [实验记录](../docs/experiments.md) 和 [日志说明](../docs/logs-and-replays.md)。

本目录移除了旧 Gym 注册、聊天模型、检索记忆及与 JEV 无关的脚本 Bot；保留的基础 `Protoss_Bot` 类及 JEV 行为实现经过原有回归测试。
