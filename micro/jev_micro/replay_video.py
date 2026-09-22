"""Render existing SMAC replays to MP4 with the matching SC2 engine.

Uses the RGB replay pipeline demonstrated by di-star-adatportuning's Windows
world_model_decoder.py. No live matches or model requests are made.
"""

import argparse
import hashlib
import io
import json
import math
import os
from pathlib import Path
import time

import imageio_ffmpeg
import mpyq
import numpy as np
from PIL import Image
from absl import flags, logging
from pysc2 import run_configs
from pysc2.run_configs.lib import Version
from s2clientprotocol import common_pb2 as common
from s2clientprotocol import sc2api_pb2 as sc

from .catalog import map_source
from .windows_session import active_display


LOOPS_PER_SECOND = 22.4


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2,
                               allow_nan=False) + "\n", encoding="utf-8")


def logged_timeline(path, episode_filter=None):
    """Read timing/outcomes from the existing log without changing it."""
    checks, episodes = {}, {}
    if path.is_file():
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                event = json.loads(line)
                number = event.get("episode")
                if episode_filter is not None and number != episode_filter:
                    continue
                if event["event"] == "observation" and number not in episodes:
                    loop = event["payload"]["state"]["game_loop"]
                    episodes[number] = {"episode": number, "start_loop": loop,
                                        "start_seconds": loop / LOOPS_PER_SECOND}
                elif event["event"] == "step":
                    loop = event["game_loop"]
                    times = event.get("evaluation_observation_loops")
                    if times:
                        # Realtime clients can observe adjacent loops. Compare each
                        # side at its actual sample time rather than mixing clocks.
                        allied_keys = {"allies_alive", "allied_health", "allied_shield"}
                        for key, value in event["evaluation"].items():
                            side = "allies" if key in allied_keys else "enemies"
                            checks.setdefault(times[side], {})[key] = value
                    else:
                        checks[loop] = event["evaluation"]
                    if number in episodes:
                        episodes[number]["end_loop"] = loop
                        episodes[number]["end_seconds"] = loop / LOOPS_PER_SECOND
    for number, episode in episodes.items():
        summary_path = path.parent / f"episode-{number:03d}" / "summary.json"
        if summary_path.is_file():
            episode["result"] = json.loads(summary_path.read_text(encoding="utf-8")).get("status")
    return checks, list(episodes.values())


def battle_totals(observation):
    units = observation.raw_data.units
    allies = [unit for unit in units if unit.owner == 1 and unit.health > 0]
    enemies = [unit for unit in units if unit.owner == 2 and unit.health > 0]
    return {"allies_alive": len(allies), "enemies_alive": len(enemies),
            "allied_health": round(sum(unit.health for unit in allies), 2),
            "enemy_health": round(sum(unit.health for unit in enemies), 2),
            "allied_shield": round(sum(unit.shield for unit in allies), 2),
            "enemy_shield": round(sum(unit.shield for unit in enemies), 2)}


def camera_target(observation, fallback):
    units = [unit for unit in observation.raw_data.units
             if unit.owner in (1, 2) and unit.health > 0]
    if not units:
        return fallback
    return ((min(unit.pos.x for unit in units) + max(unit.pos.x for unit in units)) / 2,
            (min(unit.pos.y for unit in units) + max(unit.pos.y for unit in units)) / 2)


def validate_video(path, expected_size, expected_frames):
    """Decode every encoded frame to detect incomplete/unreadable MP4s."""
    reader = imageio_ffmpeg.read_frames(str(path))
    meta = next(reader)
    count = 0
    try:
        if tuple(meta["size"]) != tuple(expected_size):
            raise RuntimeError(f"Encoded size mismatch: {meta['size']}")
        for frame in reader:
            if len(frame) != expected_size[0] * expected_size[1] * 3:
                raise RuntimeError("Truncated decoded video frame")
            count += 1
    finally:
        reader.close()
    if count != expected_frames:
        raise RuntimeError(f"Encoded frame count mismatch: {count} != {expected_frames}")
    return {"decoded_frames": count, "size": list(meta["size"]),
            "fps": meta["fps"], "duration_seconds": meta["duration"],
            "codec": meta.get("codec"), "pixel_format": meta.get("pix_fmt")}


def export(args):
    source = args.replay.resolve(strict=True)
    output = args.output.resolve()
    partial = output.with_name(output.stem + ".partial.mp4")
    for path in (output, partial, output.with_suffix(".json"), output.with_suffix(".frames.jsonl")):
        if path.exists():
            raise FileExistsError(f"Refusing to overwrite an existing output: {path}")
    output.parent.mkdir(parents=True, exist_ok=True)
    run_dir = source.parent.parent
    settings_path = run_dir / "run.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.is_file() else {}
    map_name = args.map or settings.get("map")
    if not map_name:
        raise ValueError("Pass --map when the replay has no adjacent run.json")
    map_path = map_source(map_name)
    map_data = map_path.read_bytes()
    map_sha = hashlib.sha256(map_data).hexdigest()
    if settings.get("map_sha256") and settings["map_sha256"] != map_sha:
        raise ValueError("Local map differs from the map used for the recorded game")
    replay_data = source.read_bytes()
    replay_sha = hashlib.sha256(replay_data).hexdigest()
    archive = mpyq.MPQArchive(io.BytesIO(replay_data))
    metadata = json.loads(archive.read_file("replay.gamemetadata.json"))
    version = Version(".".join(metadata["GameVersion"].split(".")[:3]),
                      int(metadata["BaseBuild"][4:]), metadata["DataVersion"], None)
    os.environ["SC2PATH"] = str(args.sc2_path.resolve(strict=True))
    if not flags.FLAGS.is_parsed():
        flags.FLAGS(["jev-replay-video", "--sc2_timeout=45"])
    logging.set_verbosity(logging.WARNING)
    config = run_configs.get()
    # The vendored version table predates 5.0.16. Use the replay's exact build
    # and data hash, including hotfixes, instead of guessing from that table.
    config.version = version
    options = sc.InterfaceOptions(raw=True, score=True)
    options.render.width = 24
    options.render.resolution.x, options.render.resolution.y = args.width, args.height
    options.render.minimap_resolution.x = options.render.minimap_resolution.y = 128
    episode_filter = int(source.parent.name.split("-")[-1]) if settings.get("realtime") else None
    checks, episodes = logged_timeline(run_dir / "events.jsonl", episode_filter)
    checked_loops, mismatches = [], []
    fps = LOOPS_PER_SECOND / args.step_mul
    start_time = time.monotonic()
    count, last_loop, thumbnail_saved = 0, 0, False
    writer = None
    print(f"Render {map_name}: {source} -> {output}", flush=True)
    try:
        with active_display(), config.start(
                want_rgb=True, full_screen=False, window_size=(args.width, args.height),
                window_loc=(0, 0)) as controller, output.with_suffix(".frames.jsonl").open(
                    "w", encoding="utf-8") as trace:
            info = controller.replay_info(replay_data)
            total_loops = info.game_duration_loops
            print(f"SC2 {info.game_version}; {total_loops} loops; {fps:g} fps", flush=True)
            controller.start_replay(sc.RequestStartReplay(
                replay_data=replay_data, map_data=map_data, options=options,
                observed_player_id=1 if args.team_vision else 0,
                disable_fog=not args.team_vision))
            initial, _ = controller.observe(disable_fog=not args.team_vision)
            game_info = controller.game_info()
            center = (game_info.start_raw.map_size.x / 2, game_info.start_raw.map_size.y / 2)
            center = camera_target(initial.observation, center)
            previous_observation = initial.observation
            writer = imageio_ffmpeg.write_frames(
                str(partial), (args.width, args.height), fps=fps, codec="libx264",
                pix_fmt_in="rgb24", pix_fmt_out="yuv420p", macro_block_size=1,
                quality=6, ffmpeg_timeout=30,
                output_params=["-preset", "fast", "-movflags", "+faststart"])
            writer.send(None)
            next_progress = time.monotonic() + 15
            while last_loop < total_loops:
                target = camera_target(previous_observation, center)
                smoothing = 1 - 0.8 ** args.step_mul
                center = tuple(old + smoothing * (new - old) for old, new in zip(center, target))
                controller.observer_act(sc.ObserverAction(camera_move=sc.ActionObserverCameraMove(
                    world_pos=common.Point2D(x=center[0], y=center[1]), distance=args.camera_distance)))
                controller.step(min(args.step_mul, total_loops - last_loop))
                response, _ = controller.observe(disable_fog=not args.team_vision)
                observation = response.observation
                loop = observation.game_loop
                if loop <= last_loop:
                    if response.player_result:
                        break
                    raise RuntimeError(f"Replay stopped advancing at loop {loop}")
                frame = observation.render_data.map
                if (frame.size.x, frame.size.y, frame.bits_per_pixel) != (args.width, args.height, 24):
                    raise RuntimeError(f"Unexpected RGB buffer format at loop {loop}: {frame.size}")
                if len(frame.data) != args.width * args.height * 3:
                    raise RuntimeError(f"Truncated RGB buffer at loop {loop}")
                writer.send(frame.data)
                if not thumbnail_saved and loop >= min(80, total_loops // 2):
                    rgb = np.frombuffer(frame.data, dtype=np.uint8).reshape(args.height, args.width, 3)
                    Image.fromarray(rgb).save(output.with_suffix(".png"))
                    thumbnail_saved = True
                totals = battle_totals(observation)
                if not args.team_vision and loop in checks:
                    expected = checks[loop]
                    if any(not math.isclose(totals[key], value, abs_tol=0.02)
                           for key, value in expected.items()):
                        mismatches.append({"game_loop": loop, "expected": expected, "rendered": totals})
                    checked_loops.append(loop)
                trace.write(json.dumps({"frame_index": count, "game_loop": loop,
                    "camera_center": center, "battle": totals}, allow_nan=False) + "\n")
                count += 1
                last_loop, previous_observation = loop, observation
                if time.monotonic() >= next_progress:
                    print(f"  {count} frames, loop {loop}/{total_loops}", flush=True)
                    next_progress = time.monotonic() + 15
                if response.player_result:
                    break
            writer.close()
            writer = None
        expected_checks = [loop for loop in checks if loop <= last_loop and loop % args.step_mul == 0]
        result = {"source_replay": str(source), "source_replay_sha256": replay_sha,
            "source_map": str(map_path.resolve()), "source_map_sha256": map_sha,
            "source_events": str(run_dir / "events.jsonl"), "map": map_name,
            "game_version": metadata["GameVersion"], "data_version": metadata["DataVersion"],
            "client_branch": settings.get("client_branch"), "video": str(output),
            "resolution": [args.width, args.height], "fps": fps, "audio": False,
            "view": "team_vision" if args.team_vision else "all_units_no_fog",
            "camera": "smoothed center of living armies", "camera_distance": args.camera_distance,
            "frame_step_loops": args.step_mul, "replay_duration_loops": total_loops,
            "first_frame_loop": min(args.step_mul, total_loops), "last_frame_loop": last_loop,
            "frames": count, "video_duration_seconds": count / fps,
            "episodes": [ep for ep in episodes if ep["start_loop"] < total_loops],
            "log_verification": {"checked_steps": len(checked_loops),
                "expected_steps": len(expected_checks), "mismatches": mismatches},
            "original_replay_unchanged": hashlib.sha256(source.read_bytes()).hexdigest() == replay_sha}
        if total_loops - last_loop > args.step_mul:
            raise RuntimeError(f"Replay ended early at {last_loop}/{total_loops}")
        if mismatches:
            write_json(output.with_suffix(".verification-failed.json"), result)
            raise RuntimeError(f"Replay differs from original battle log at {len(mismatches)} steps")
        if not result["original_replay_unchanged"]:
            raise RuntimeError("Source replay changed while rendering")
        result["video_verification"] = validate_video(partial, (args.width, args.height), count)
        partial.rename(output)
        result["export_seconds"] = round(time.monotonic() - start_time, 2)
        write_json(output.with_suffix(".json"), result)
        print(f"Saved {output}: {count} frames; {len(checked_loops)} battle checks matched", flush=True)
        return result
    finally:
        if writer is not None:
            writer.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--map")
    parser.add_argument("--sc2-path", type=Path, default=Path(r"C:\game\StarCraft II"))
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--step-mul", type=int, default=1)
    parser.add_argument("--camera-distance", type=float, default=24)
    parser.add_argument("--team-vision", action="store_true")
    args = parser.parse_args()
    if args.width <= 0 or args.height <= 0 or args.width % 2 or args.height % 2 or args.step_mul < 1:
        parser.error("Use positive even video dimensions and a positive step-mul")
    if args.output.suffix.lower() != ".mp4":
        parser.error("Output must have the .mp4 extension")
    export(args)


if __name__ == "__main__":
    main()
