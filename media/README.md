# Winning games and replays / 胜局视频与回放

[English](#english) | [简体中文](#简体中文) · [Read the paper / 在线阅读论文](../paper/JEV-Star.pdf)

## Videos / 视频

Play the full winning games directly below. **22.4 fps, original speed, complete matches.**

点击下方播放器即可观看完整胜局，**22.4 fps、原速播放**。

### M01 · Macro VeryHard / Elite victory / 宏观最高非作弊难度胜局

macro-v2.1 / Astra + JEV · 12:47.90

https://github.com/user-attachments/assets/5cab5e0a-e8c4-43b8-a504-96f916f73e2a

### M02 · Macro Easy victory / 宏观 Easy 胜局

earlier Astra + JEV macro · 15:04.20

https://github.com/user-attachments/assets/2beaa558-67d5-4b7f-8a8d-c29f4359ab36

### U01 · Micro mmmt victory / 微观 mmmt 胜局

C / P0 Astra + JEV / schema 7 · 00:21.43

https://github.com/user-attachments/assets/48b26ba0-63be-45c4-82a5-f4bb6814f73e

## English

All three full matches play on this GitHub page. The committed MP4 files preserve the full duration, 22.4 fps cadence and original playback speed. Macro videos use 1280 × 720 for inline viewing; the micro video uses 1600 × 900. The original higher-bitrate recordings remain in the [media archive](https://github.com/histmeisah/Jev_Star/releases/tag/media-20260923).

| Recording | Repository video | Original replay |
| --- | --- | --- |
| M01 · Macro: VeryHard / Elite, seed 1 | [MP4 file](videos/M01-full-22p4.mp4) | [SC2Replay](replays/macro/macro-v2-20260922-veryhard-003.SC2Replay) |
| M02 · Macro: Easy, seed 1 | [MP4 file](videos/M02-full-22p4.mp4) | [SC2Replay](replays/macro/astra-jev-20m-006.SC2Replay) |
| U01 · Micro: mmmt, P0 Astra + JEV, episode 1 | [MP4 file](videos/U01-mmmt-22p4.mp4) | [SC2Replay](replays/micro/C-mmmt-episode-001.SC2Replay) |

The collection includes 6 macro winning replays and 16 micro winning replays (A: 3, B: 6, C: 7). The three B `pvt_large` wins are development-map results. These are selected victories; see the [complete evaluation](../docs/experiments.md) for overall results.

Micro replay files may include earlier episodes from the same client. Use the table's winning interval to locate the episode. The videos were rendered with SC2 5.0.16.97563, Asia installation (`kr`). Install matching maps with `python scripts/install_maps.py all`. The micro video was checked against all 60 original decision steps.

[Manifest and video metadata](manifest.json) · [Repository video and replay SHA-256 checksums](SHA256SUMS.txt)

## 简体中文

上方 3 个播放器可在 GitHub 页面内直接播放完整胜局。视频文件同时收录于仓库 `media/videos/`，保留完整时长、22.4 fps 和原速。宏观在线播放版为 1280 × 720，微观为 1600 × 900；原始高码率录像保留在[媒体归档](https://github.com/histmeisah/Jev_Star/releases/tag/media-20260923)。

另有 22 份原始胜局 replay：宏观 6 份，微观 A 纯 JEV 3 份、B 旧双模型 6 份、C P0 双模型 7 份。B 中 pvt_large 的 3 局为开发地图。完整实验成绩及版本边界见[实验记录](../docs/experiments.md)。

微观 replay 可能包含同一客户端之前的对局，应按下表时间区间定位胜局。视频由 SC2 5.0.16.97563 亚服 kr 安装渲染；在仓库根目录执行 `python scripts/install_maps.py all` 安装对应地图。微观展示视频的 60 个决策步均与原始日志核对一致。

[完整媒体清单](manifest.json) · [仓库视频与 replay 的 SHA-256](SHA256SUMS.txt)

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
