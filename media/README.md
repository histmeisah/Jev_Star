# Victory videos and replays / 胜局视频与回放

[English](#english) | [简体中文](#简体中文)

## English

[Download all release assets](https://github.com/histmeisah/Jev_Star/releases/tag/media-20260923) · [Replay bundle](https://github.com/histmeisah/Jev_Star/releases/download/media-20260923/jev-star-winning-replays-20260923.zip) · [Machine-readable manifest](manifest.json)

Three reviewed winning videos, rendered from original replays at **22.4 fps and 1× speed**. Videos have no audio. Click a thumbnail or MP4 link to open or download the video.

| Video | Version / scenario | Duration | Resolution | Downloads |
| --- | --- | ---: | --- | --- |
| M01 | Macro: VeryHard / Elite, seed 1<br>macro-v2.1 / Astra + JEV | 12:47.90 | 1600 × 900 | [MP4 · 328.3 MB](https://github.com/histmeisah/Jev_Star/releases/download/media-20260923/M01-macro-veryhard-seed1-22p4.mp4) · [Replay](replays/macro/macro-v2-20260922-veryhard-003.SC2Replay) |
| M02 | Macro: Easy, seed 1<br>earlier Astra + JEV macro | 15:04.20 | 1280 × 720 | [MP4 · 239.8 MB](https://github.com/histmeisah/Jev_Star/releases/download/media-20260923/M02-macro-easy-seed1-22p4.mp4) · [Replay](replays/macro/astra-jev-20m-006.SC2Replay) |
| U01 | Micro: mmmt, P0 Astra + JEV, episode 1<br>C / P0 Astra + JEV / schema 7 | 00:21.43 | 1600 × 900 | [MP4 · 18.7 MB](https://github.com/histmeisah/Jev_Star/releases/download/media-20260923/U01-micro-mmmt-p0-episode1-22p4.mp4) · [Replay](replays/micro/C-mmmt-episode-001.SC2Replay) |

The macro recordings use an observer camera and include a side panel showing Astra plans and JEV orders. The micro recording shows the P0 `mmmt` episode-1 victory; its 60 decision steps were checked against the original run log.

This collection contains **6 macro winning replays and 16 micro winning replays** (A: 3, B: 6, C: 7). The three B `pvt_large` wins are development-map results. These selected victories do not represent the overall win rate; see the [complete experiment results](../docs/experiments.md).

Use the matching SC2 client and maps to open the native replay files; the videos were rendered with SC2 **5.0.16.97563**, Asia installation (`kr`). Maps are included under `macro/Maps` and `micro/SMAC_HARD_maps`; install them with `python scripts/install_maps.py all` from the repository root.

A micro replay can contain earlier episodes from the same client session. The replay table below and the manifest give the winning episode's interval; do not treat every replay file as a standalone single match. Frame positions use 22.4 game loops per second. Replay SHA-256 values are in [SHA256SUMS.txt](SHA256SUMS.txt); MP4 hashes are in the manifest and the release checksum file.

## 简体中文

[发布页与全部附件](https://github.com/histmeisah/Jev_Star/releases/tag/media-20260923) · [胜局 replay 合集](https://github.com/histmeisah/Jev_Star/releases/download/media-20260923/jev-star-winning-replays-20260923.zip) · [完整清单](manifest.json)

本次公开 **3 部已核验胜局视频**：M01 宏观 VeryHard/Elite、M02 宏观 Easy、U01 微观 P0 `mmmt` 第 1 局。均从原始 replay 渲染为 **22.4 fps、原速播放**，无音轨；点击上表 MP4 链接或下方缩略图观看、下载。

宏观画面带有观察者镜头、Astra 计划与 JEV 指令侧栏；微观胜局已与原始日志逐步核对，60 个决策步全部一致。每个视频都附对应原始 replay，视频和 replay 的 SHA-256 可用于校验。

另附 **22 份原始胜局 replay：宏观 6 份，微观 16 份（A 纯 JEV 3 份、B 旧双模型 6 份、C P0 双模型 7 份）**。B 中 `pvt_large` 的 3 局属于开发地图。这里是胜局展示，整体成绩和版本边界见[完整实验结果](../docs/experiments.md)。

微观 replay 可能累计同一客户端之前的对局，需按下表的起止时间定位胜局；不能把整个文件都视为一场胜局。视频由 SC2 5.0.16.97563 亚服 kr 安装渲染；播放原生 replay 需匹配的客户端与地图，可在仓库根目录运行 `python scripts/install_maps.py all` 安装地图。

### Video previews / 视频预览

**M01 · Macro: VeryHard / Elite, seed 1 / 宏观 · VeryHard / Elite · seed 1**

[![Macro: VeryHard / Elite, seed 1](posters/M01.jpg)](https://github.com/histmeisah/Jev_Star/releases/download/media-20260923/M01-macro-veryhard-seed1-22p4.mp4)

**M02 · Macro: Easy, seed 1 / 宏观 · Easy · seed 1**

[![Macro: Easy, seed 1](posters/M02.jpg)](https://github.com/histmeisah/Jev_Star/releases/download/media-20260923/M02-macro-easy-seed1-22p4.mp4)

**U01 · Micro: mmmt, P0 Astra + JEV, episode 1 / 微观 · mmmt · 第 1 局**

[![Micro: mmmt, P0 Astra + JEV, episode 1](posters/U01.jpg)](https://github.com/histmeisah/Jev_Star/releases/download/media-20260923/U01-micro-mmmt-p0-episode1-22p4.mp4)

### Original winning replays / 原始胜局 replay

| ID | Track / version | Map / difficulty | Episode / seed | Winning interval (s) | Replay |
| --- | --- | --- | --- | --- | --- |
| R01 | Macro / 宏观 | Altitude LE / Easy | seed 1 | Full match / 完整对局 | [SC2Replay](replays/macro/astra-jev-20m-006.SC2Replay) |
| R02 | Macro / 宏观 | Altitude LE / Easy | seed 1 | Full match / 完整对局 | [SC2Replay](replays/macro/astra-jev-20m-004.SC2Replay) |
| R03 | Macro / 宏观 | Altitude LE / Hard | seed 1 | Full match / 完整对局 | [SC2Replay](replays/macro/macro-v2-20260922-hard-002.SC2Replay) |
| R04 | Macro / 宏观 | Altitude LE / Harder | seed 1 | Full match / 完整对局 | [SC2Replay](replays/macro/macro-v2-20260922-harder-001.SC2Replay) |
| R05 | Macro / 宏观 | Altitude LE / VeryHard | seed 1 | Full match / 完整对局 | [SC2Replay](replays/macro/macro-v2-20260922-veryhard-003.SC2Replay) |
| R06 | Macro / 宏观 | Altitude LE / VeryHard | seed 2 | Full match / 完整对局 | [SC2Replay](replays/macro/macro-v2-20260922-veryhard-004.SC2Replay) |
| R07 | Micro A / schema 3 | 2s3z | 2 | 16.205–37.634 | [SC2Replay](replays/micro/A-2s3z-episode-002.SC2Replay) |
| R08 | Micro A / schema 3 | 2s3z | 3 | 37.768–65.268 | [SC2Replay](replays/micro/A-2s3z-episode-003.SC2Replay) |
| R09 | Micro A / schema 3 | bane_vs_bane | 2 | 14.330–21.473 | [SC2Replay](replays/micro/A-bane_vs_bane-episode-002.SC2Replay) |
| R10 | Micro B / schema 5 | 2s3z | 3 | 37.054–55.625 | [SC2Replay](replays/micro/B-2s3z-episode-003.SC2Replay) |
| R11 | Micro B / schema 5 | 3s5z | 3 | 47.054–70.982 | [SC2Replay](replays/micro/B-3s5z-episode-003.SC2Replay) |
| R12 | Micro B / schema 5 | bane_vs_bane | 2 | 6.473–15.759 | [SC2Replay](replays/micro/B-bane_vs_bane-episode-002.SC2Replay) |
| R13 | Micro B / schema 5 | pvt_large (development / 开发) | 1 | 0.000–64.286 | [SC2Replay](replays/micro/B-pvt_large-episode-001.SC2Replay) |
| R14 | Micro B / schema 5 | pvt_large (development / 开发) | 2 | 64.420–122.634 | [SC2Replay](replays/micro/B-pvt_large-episode-002.SC2Replay) |
| R15 | Micro B / schema 5 | pvt_large (development / 开发) | 3 | 122.768–189.196 | [SC2Replay](replays/micro/B-pvt_large-episode-003.SC2Replay) |
| R16 | Micro C / schema 7 | 2c_vs_64zg | 2 | 18.705–40.134 | [SC2Replay](replays/micro/C-2c_vs_64zg-episode-002.SC2Replay) |
| R17 | Micro C / schema 7 | 2s3z | 2 | 19.777–39.420 | [SC2Replay](replays/micro/C-2s3z-episode-002.SC2Replay) |
| R18 | Micro C / schema 7 | 8m | 3 | 20.982–31.696 | [SC2Replay](replays/micro/C-8m-episode-003.SC2Replay) |
| R19 | Micro C / schema 7 | MMM | 2 | 26.920–41.920 | [SC2Replay](replays/micro/C-MMM-episode-002.SC2Replay) |
| R20 | Micro C / schema 7 | bane_vs_bane | 1 | 0.000–6.071 | [SC2Replay](replays/micro/C-bane_vs_bane-episode-001.SC2Replay) |
| R21 | Micro C / schema 7 | mmmt | 1 | 0.000–21.429 | [SC2Replay](replays/micro/C-mmmt-episode-001.SC2Replay) |
| R22 | Micro C / schema 7 | mmmt | 2 | 21.562–46.920 | [SC2Replay](replays/micro/C-mmmt-episode-002.SC2Replay) |
