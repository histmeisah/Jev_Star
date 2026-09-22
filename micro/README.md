# SMAC-Hard 微操

当前接口为 `p0-v1`：纯 JEV 为 schema 6，Astra＋JEV 为 schema 7。每图先由 Astra 生成一份计划，三局复用；JEV 为每个存活单位回答一道 Choice。小队共用一次请求，大队按预算分批，所有批次使用同一暂停中的观察。

默认 `realtime=False`、`step_mul=8`、seed 1、base 脚本对手；等待模型时实际游戏时钟保持不变。死亡单位由环境填 no-op，存活单位动作全部由 JEV 选择。原先的实时探测通过 `micro-realtime` 单独保留，未作为当前基线。

| 动作 | 含义 |
| --- | --- |
| 0 | 死亡单位 no-op，不提供给存活单位 |
| 1 | Stop，取消当前订单 |
| 2 / 3 / 4 / 5 | 北 / 南 / 东 / 西，各移动 2 个世界单位 |
| 6+j | 攻击可见敌人 e{j}；医疗机改为治疗友军 u{j} |

保留 SMAC 中心距离 6 的攻击候选限制，原生射程另行描述；技能、变形、任意坐标和目标预约没有加入动作空间。

四项 P0：公共地图背景；明确的射程和合法候选语义；分批中完整的队友核心字段；将 SC2 原生 ResponseAction 和观察错误回传到下一轮。命令被接受不等于已经造成伤害。

| 文件 | 职责 |
| --- | --- |
| [policy.py](jev_micro/policy.py) | 结构化状态、单位问题、批次与动作解码 |
| [planner.py](jev_micro/planner.py) | 每图 Astra 计划及复用 |
| [scenario.py](jev_micro/scenario.py) | 公开地图先验与静态地形 |
| [feedback.py](jev_micro/feedback.py) | SC2 原生命令和错误映射 |
| [run.py](jev_micro/run.py) | 完整对局、日志、时钟核对与 replay |
| [suite.py](jev_micro/suite.py) | 全图批测、失败保留与恢复 |
| [catalog.py](jev_micro/catalog.py) | 35 张地图及两个开发场景标记 |

从仓库根目录启动全图三局：

```powershell
py -3.10 jev_star.py micro-suite --all-maps --policies jev --episodes 3 --seed 1 --step-mul 8 --planner codex --planner-model gpt-6-astra --planner-effort medium --planner-timeout 180 --request-timeout 15 --batch-concurrency 4 --api-retries 2 --output-dir jev_runs/my-sweep
```

恢复同一配置时增加 `--resume` 并使用相同输出目录。已完成的三局地图复用，失败尝试保留并创建新的重试目录；有效结果以汇总中的 `summary_file` 为准。切换接口、计划或参数应创建新的实验目录。

`SMAC_HARD_maps/` 集中保存全部 35 张注册地图，包括原上游旧目录中的地图。环境使用本目录自带且经修改的 `pysc2/`；不要用 pip 安装的另一份 PySC2 替代它。

结果见 [实验记录](../docs/experiments.md)，安装和密钥配置见 [总说明](../README.md)。
