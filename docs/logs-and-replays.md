# 日志与回放

每次运行使用独立输出目录，已有目录不被单图入口静默覆盖。凭据通过环境变量或被忽略的本地配置读取，不写入日志。

| 文件 | 内容 |
| --- | --- |
| `run.json` | 模型、模式、地图、种子、时间配置及版本 |
| `events.jsonl` | 完整的请求、回复、选择、执行和终局事件，保留原时序 |
| `summary.json` | 结果、错误、用量、耗时和 replay 路径 |
| `planner/` | Astra schema、原计划及规划记录 |
| `game.SC2Replay` | SC2 原生 replay；微观位于逐局目录 |

宏观还生成 `progress.json`、`timeline.log`、`engine.log`、`report.html` 和决策表。微观保存场景背景、每次固定步进核对，以及每单位对应的 SC2 原生 ResponseAction/观察错误。`Success` 表示命令被接受，不等于已经发射或命中。

宏观离线报告可从已有日志重建：

```powershell
py -3.10 jev_star.py macro-report jev_runs/my-run
```

微观批测通过 `micro-suite` 生成汇总和逐图对照；存在重试时只使用汇总选定的 `summary_file`，保留未采用尝试的费用和失败原因。

## 导出视频

微观可以使用仓库内的 PySC2、已安装的匹配版本 SC2 及可选视频依赖渲染 replay。先运行 `python scripts/setup_environment.py micro --video`，再通过以下命令查看参数：

```powershell
py -3.10 jev_star.py micro-video --help
```

宏观提供已有的 DI-star Windows RGB 解码器适配入口 `macro-video`，需另外安装 DI-star 解码器并指定 `--decoder-root`；该外部框架没有打包进本仓库。可选图像与编码依赖通过 `python scripts/setup_environment.py macro --video` 安装。

导出使用观察者相机，不影响已完成对局。一个微观 replay 可能包含同一客户端连续 reset 的多局；分析时用事件日志中的 episode 起止帧定位，不能假设每份文件只含单局。

新导出的日志、视频和 replay 均保持本地产物；`.gitignore` 会排除它们。论文中的少量截图和固定图表属于明确保留的研究材料。
