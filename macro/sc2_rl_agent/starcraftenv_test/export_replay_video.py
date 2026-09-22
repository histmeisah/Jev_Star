"""Render SC2 replays as MP4 with the DI-star Windows RGB decoder and an observer camera."""

import argparse
from functools import lru_cache
import hashlib
import html
import json
import logging
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from .utils.replay_camera import ReplayCamera, distance

LOOPS_PER_SECOND = 22.4


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('replay', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--decoder-root', type=Path,
                        default=Path(os.environ.get('DISTAR_ROOT', 'external/di-star')),
                        help='External DI-star checkout containing the Windows RGB decoder; also accepts DISTAR_ROOT')
    parser.add_argument('--sc2-path', type=Path, default=Path(r'C:\game\StarCraft II'))
    parser.add_argument('--ffmpeg', type=Path)
    parser.add_argument('--events', type=Path, help='Defaults to events.jsonl next to the replay')
    parser.add_argument('--player', type=int, default=1)
    parser.add_argument('--camera', choices=['auto', 'player', 'fixed'], default='auto')
    parser.add_argument('--fog', action='store_true', help='Restrict video to the selected player perspective')
    parser.add_argument('--render-channel-order', choices=['bgr', 'rgb'], default='bgr',
                        help='DI-star Windows buffers use OpenCV BGR order; convert to RGB before composition')
    parser.add_argument('--width', type=int, default=1280, help='Total video width including 320px information panel')
    parser.add_argument('--height', type=int, default=720)
    parser.add_argument('--step-mul', type=int, default=2, help='Sample every N game loops (2 = 11.2 frames per game second)')
    parser.add_argument('--speed', type=float, default=1, help='Playback speed; all sampled frames are retained')
    parser.add_argument('--start', type=float, default=0, help='Start at this game second')
    parser.add_argument('--duration', type=float, help='Optional clip duration in game seconds')
    args = parser.parse_args()
    for key in ('speed', 'duration'):
        value = getattr(args, key)
        if value is not None and (not math.isfinite(value) or value <= 0):
            parser.error(f'--{key} must be finite and positive')
    if not math.isfinite(args.start) or args.start < 0 or args.step_mul < 1:
        parser.error('--start must be nonnegative and --step-mul must be positive')
    if args.width < 960 or args.height < 720 or args.width % 2 or args.height % 2:
        parser.error('Use even dimensions at least 960x720')
    return args


def find_ffmpeg(explicit):
    if explicit:
        return str(explicit.resolve(strict=True))
    command = shutil.which('ffmpeg')
    if command:
        return command
    workspace = Path(__file__).resolve().parents[3]
    candidates = sorted((workspace / '.tools/replay-video-deps/imageio_ffmpeg/binaries').glob('ffmpeg*.exe'))
    if candidates:
        return str(candidates[-1])
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError as exc:
        raise RuntimeError('Provide --ffmpeg or install imageio-ffmpeg in the video Python environment') from exc


class Journal:
    def __init__(self, path):
        self.events, self.index, self.plan, self.action = [], 0, None, None
        if path.is_file():
            with path.open(encoding='utf-8-sig') as source:
                for line in source:
                    event = json.loads(line)
                    if event.get('event') in ('plan_accepted', 'action', 'army_intent_changed'):
                        self.events.append(event)

    def update(self, game_seconds):
        while self.index < len(self.events) and self.events[self.index].get('game_seconds', 0) <= game_seconds:
            event = self.events[self.index]
            if event['event'] == 'plan_accepted':
                self.plan = event['plan']
            elif event['event'] == 'action' and event.get('orders_submitted'):
                self.action = event
            self.index += 1
        return self.plan, self.action


@lru_cache(maxsize=1)
def panel_fonts():
    from PIL import ImageFont
    font_path = Path(r'C:\Windows\Fonts\msyh.ttc')
    if font_path.exists():
        return ImageFont.truetype(str(font_path), 16), ImageFont.truetype(str(font_path), 22)
    font = ImageFont.load_default()
    return font, font


@lru_cache(maxsize=512)
def wrapped_lines(text, width):
    font, _ = panel_fonts()
    line, lines = '', []
    for char in text:
        if font.getlength(line + char) > width:
            lines.append(line)
            line = char
        else:
            line += char
    if line:
        lines.append(line)
    return lines


def draw_panel(frame, minimap, ob, journal, reason, args):
    import numpy as np
    from PIL import Image, ImageDraw
    width = 320
    panel = Image.new('RGB', (width, args.height), '#101c2c')
    draw = ImageDraw.Draw(panel)
    font, title_font = panel_fonts()
    def write(text, y, color='#dce6ef', large=False):
        draw.text((18, y), str(text), font=title_font if large else font, fill=color)
    def wrap(text, y, max_lines):
        # Cache layout so long strategy text is not measured on every frame.
        lines = wrapped_lines(str(text), width - 36)
        for n, line in enumerate(lines[:max_lines]):
            write(line + ('...' if n == max_lines - 1 and len(lines) > max_lines else ''), y + n * 24)
    game_seconds = ob.game_loop / LOOPS_PER_SECOND
    plan, action = journal.update(game_seconds)
    player = ob.player_common
    write('ASTRA + JEV | SC2', 16, '#7bddca', True)
    write(f'{int(game_seconds)//60:02d}:{int(game_seconds)%60:02d}   |   {args.speed:g}x playback', 54)
    write('Player fog' if args.fog else 'Replay observer | fog off', 84, '#9caec4')
    write(reason, 114, '#7bddca')
    draw.line((18, 147, width - 18, 147), fill='#32445a')
    write(f'Minerals {player.minerals}    Gas {player.vespene}', 158)
    write(f'Workers {player.food_workers}    Army {player.food_army}', 188)
    write(f'Supply {player.food_used}/{player.food_cap}', 218)
    valid = plan and game_seconds < plan.get('expires_game_seconds', float('inf'))
    write(f"Astra plan #{plan['plan_id']} | {plan.get('army_posture', '')}" if valid else 'Jev | no active Astra plan', 264, '#7bddca')
    wrap(plan.get('objective', '') if valid else 'Awaiting strategic guidance', 298, 5)
    write('Latest Jev order', 425, '#7bddca')
    wrap(action.get('description', '') if action else 'Waiting', 453, 2)
    if minimap is not None:
        image = Image.fromarray(minimap).resize((192, 192))
        panel.paste(image, ((width - 192) // 2, args.height - 204))
    return np.concatenate((frame, np.asarray(panel)), axis=1)


def unit_rows(ob, types):
    rows = []
    for u in ob.raw_data.units:
        if u.display_type != 1:  # A snapshot is not a currently observed unit.
            continue
        info = types.get(u.unit_type)
        name = info.name.upper() if info else ''
        structure = bool(info and 8 in info.attributes)
        worker = name in {'PROBE', 'SCV', 'DRONE', 'MULE'}
        combat = bool(info and info.weapons and not worker) or name in {'CARRIER', 'HIGHTEMPLAR', 'INFESTOR', 'VIPER'}
        rows.append(dict(tag=u.tag, owner=u.owner, pos=(u.pos.x, u.pos.y),
                         base=name in {'NEXUS', 'COMMANDCENTER', 'ORBITALCOMMAND', 'PLANETARYFORTRESS', 'HATCHERY', 'LAIR', 'HIVE'},
                         structure=structure, combat=combat, worker=worker, build_progress=u.build_progress))
    return rows


def write_watch_page(output, metadata, chapters):
    links = []
    camera_labels = {'Base economy': '基地经济', 'Base defense': '基地防守', 'Expansion': '扩张',
                     'Engagement': '交战', 'Army movement': '主力推进', 'Remaining forces': '剩余部队'}
    for second, label in chapters:
        if second < metadata['start_game_seconds']:
            continue
        video_time = max(0, (second - metadata['start_game_seconds']) / metadata['playback_speed'])
        if video_time <= metadata['video_duration_seconds']:
            links.append(f'<button onclick="v.currentTime={video_time:.3f};v.play()">{int(second)//60:02d}:{int(second)%60:02d} {html.escape(camera_labels.get(label, label))}</button>')
    report_link = ''
    report = Path(metadata['replay']).parent / 'report.html'
    if report.is_file():
        try:
            target = os.path.relpath(report, output.parent).replace('\\', '/')
        except ValueError:
            target = report.as_uri()
        report_link = f'<a href="{html.escape(target, quote=True)}">对局日志报告</a>'
    result = next((r['result'] for r in metadata['result'] if r['player'] == metadata['player']), 0)
    result_text = {1: '胜利', 2: '失败', 3: '平局'}.get(result, '未知结果')
    page = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>Astra + Jev 星际回放</title><link rel="icon" href="data:,">
<style>body{{margin:24px;background:#0c1521;color:#e6edf7;font:16px system-ui}}main{{max-width:1380px;margin:auto}}video{{width:100%;max-height:80vh;background:black}}button,a{{background:#21334b;color:#dceefe;border:0;padding:9px 14px;margin:5px;border-radius:5px;cursor:pointer}}nav{{display:flex;flex-wrap:wrap}}p{{color:#adbed5}}</style>
<main><h1>Astra + Jev · 对局回放</h1><p>{html.escape(metadata['map'])} · {result_text} · 无音轨 · {'玩家战争迷雾' if metadata['fog'] else '观察者全图视野'} · {metadata['width']}×{metadata['height']} · {metadata['fps']:.1f} fps</p>
<video id="v" controls preload="metadata" poster="{html.escape(output.with_suffix('.first.jpg').name, quote=True)}" src="{html.escape(output.name, quote=True)}"></video>
<nav><button onclick="v.playbackRate=1">1×</button><button onclick="v.playbackRate=2">2×</button><button onclick="v.playbackRate=4">4×</button>{report_link}</nav>
<p>点击下方时间跳转。画面旁的计划按日志接受时间同步；回放观察者信息只用于观看。</p><nav>{''.join(links)}</nav></main></html>'''
    output.with_suffix('.html').write_text(page, encoding='utf-8')


def main():
    args = arguments()
    args.replay = args.replay.resolve(strict=True)
    args.output = args.output.resolve()
    if args.output.suffix.lower() != '.mp4':
        raise ValueError('--output must end in .mp4')
    partial = args.output.with_name(args.output.stem + '.partial.mp4')
    if args.output.exists() or partial.exists():
        raise FileExistsError('Use a new output filename; existing videos are never overwritten')
    decoder_root = args.decoder_root.resolve(strict=True)
    sys.path.insert(0, str(decoder_root))
    os.environ['SC2PATH'] = str(args.sc2_path.resolve(strict=True))
    from replay_process.windows_version.decode_replay_to_rgb.world_model_decoder import (
        WorldModelDecoder, _render_image_to_array)
    from s2clientprotocol import sc2api_pb2 as sc_pb

    class OwnedDecoder(WorldModelDecoder):
        # The upstream training decoder also kills every BlizzardError.exe.
        # For this review tool, clean up only the SC2 process we launched.
        def _safe_close(self):
            self._stop_window_guard_loop()
            process, self._sc2_process = self._sc2_process, None
            if process is not None:
                try:
                    process.close()
                finally:
                    if process._proc is not None and process._proc.poll() is None:
                        process._proc.kill()
                        process._proc.wait(timeout=10)
            self._controller = self._sc2_pid = self._sc2_port = self._sc2_tmp_dir = None

    args.output.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = find_ffmpeg(args.ffmpeg)
    decoder = OwnedDecoder(dict(screen_width=args.width - 320, screen_height=args.height,
                                minimap_size=224, frame_skip=args.step_mul, camera_width=32,
                                sc2_window_size=(args.width - 320, args.height),
                                protect_sc2_window=True))
    digest = hashlib.sha256(args.replay.read_bytes()).hexdigest()
    journal = Journal(args.events or args.replay.parent / 'events.jsonl')
    camera, encoder = ReplayCamera(args.player), None
    fps = LOOPS_PER_SECOND / args.step_mul * args.speed
    frames, chapters, first_loop, last_loop = 0, [], None, None
    started = time.monotonic()
    metadata = dict(status='running', replay=str(args.replay), replay_sha256=digest,
                    decoder_root=str(decoder_root), output=str(args.output), camera=args.camera,
                    fog=args.fog, player=args.player, width=args.width, height=args.height,
                    render_channel_order=args.render_channel_order,
                    fps=fps, step_mul=args.step_mul, playback_speed=args.speed, audio=False)
    camera_path = args.output.with_suffix('.camera.jsonl')
    try:
        version, replay_meta = decoder.get_replay_meta(str(args.replay))
        build = int(replay_meta['BaseBuild'].removeprefix('Base'))
        # The metadata pins the exact hotfix; do not silently use another binary.
        binary = args.sc2_path / 'Versions' / f'Base{build}' / 'SC2_x64.exe'
        if sys.platform == 'win32' and not binary.is_file():
            raise FileNotFoundError(f'Replay requires SC2 build {build}: {binary}')
        if not decoder._ensure_sc2('latest', build, replay_meta['DataVersion']):
            raise RuntimeError('Could not start the matching SC2 replay renderer')
        controller = decoder._controller
        info = controller.replay_info(str(args.replay))
        if args.player not in [p.player_info.player_id for p in info.player_info]:
            raise ValueError('The selected player is not in this replay')
        start_loop = int(round(args.start * LOOPS_PER_SECOND))
        end_loop = info.game_duration_loops
        if start_loop >= end_loop:
            raise ValueError('--start is beyond the end of the replay')
        if args.duration is not None:
            end_loop = min(end_loop, start_loop + int(round(args.duration * LOOPS_PER_SECOND)))
        controller.start_replay(sc_pb.RequestStartReplay(replay_path=str(args.replay),
                                 options=decoder._interface, observed_player_id=args.player, disable_fog=not args.fog))
        types = {u.unit_id: u for u in controller.data_raw().units}
        decoder._guard_sc2_window(retries=4, delay=.1)
        if start_loop:
            controller.step(start_loop)
        observation = controller.observe(disable_fog=not args.fog)
        if args.camera == 'player':
            action = sc_pb.ObserverAction()
            action.camera_follow_player.player_id = args.player
            controller.observer_act(action)
        command = [ffmpeg, '-hide_banner', '-loglevel', 'warning', '-n', '-f', 'rawvideo',
                   '-pixel_format', 'rgb24', '-video_size', f'{args.width}x{args.height}',
                   '-framerate', str(fps), '-i', 'pipe:0', '-an', '-c:v', 'libx264',
                   '-preset', 'veryfast', '-crf', '22', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(partial)]
        with args.output.with_suffix('.ffmpeg.log').open('wb') as err, camera_path.open('x', encoding='utf-8') as camera_log:
            encoder = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=err,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            last_camera, last_camera_loop, last_reason, last_report = None, -100, None, time.monotonic()
            while observation.observation.game_loop < end_loop:
                ob = observation.observation
                game_time = ob.game_loop / LOOPS_PER_SECOND
                reason = 'Recorded player camera' if args.camera == 'player' else camera.reason
                if args.camera != 'player' and (args.camera != 'fixed' or last_camera is None) and ob.game_loop - last_camera_loop >= 11:
                    position, reason = camera.choose(unit_rows(ob, types), game_time)
                    if position is not None and (last_camera is None or distance(last_camera, position) > 1 or reason != last_reason):
                        action = sc_pb.ObserverAction()
                        action.camera_move.world_pos.x, action.camera_move.world_pos.y = position
                        action.camera_move.distance = 32
                        controller.observer_act(action)
                        record = dict(game_loop=ob.game_loop, game_seconds=game_time, position=position, reason=reason)
                        camera_log.write(json.dumps(record) + '\n')
                        if reason != last_reason:
                            chapters.append((game_time, reason))
                        last_camera, last_reason = position, reason
                        # Refresh the render after the observer command without changing game time.
                        observation = controller.observe(disable_fog=not args.fog)
                        ob = observation.observation
                    last_camera_loop = ob.game_loop
                rd = ob.render_data
                if not rd.map.data:
                    raise RuntimeError(f'Missing RGB frame at game loop {ob.game_loop}')
                frame, _ = _render_image_to_array(rd.map, args.height, args.width - 320, 'rgb', allow_resize=False)
                minimap = None
                if rd.minimap.data:
                    minimap, _ = _render_image_to_array(rd.minimap, 224, 224, 'minimap', allow_resize=True)
                if args.render_channel_order == 'bgr':
                    frame = frame[:, :, ::-1].copy()
                    if minimap is not None:
                        minimap = minimap[:, :, ::-1].copy()
                combined = draw_panel(frame, minimap, ob, journal, reason, args)
                encoder.stdin.write(combined.tobytes())
                if first_loop is None:
                    first_loop = ob.game_loop
                last_loop = ob.game_loop
                frames += 1
                if frames == 1 or (reason in ('Engagement', 'Base defense') and not args.output.with_suffix('.combat.jpg').exists()):
                    from PIL import Image
                    Image.fromarray(combined).save(args.output.with_suffix('.first.jpg' if frames == 1 else '.combat.jpg'))
                if observation.player_result:
                    break
                if time.monotonic() - last_report >= 10:
                    print(f'Rendered {game_time / 60:.1f}/{end_loop / LOOPS_PER_SECOND / 60:.1f} game minutes; {frames} frames; camera={reason}', flush=True)
                    last_report = time.monotonic()
                controller.step(min(args.step_mul, end_loop - ob.game_loop))
                observation = controller.observe(disable_fog=not args.fog)
                if observation.observation.game_loop <= last_loop:
                    if observation.player_result:
                        break
                    raise RuntimeError('Replay stopped advancing')
            encoder.stdin.close()
            if encoder.wait(timeout=60) != 0 or frames == 0:
                raise RuntimeError('Video encoder failed; see the .ffmpeg.log file')
        args.output.parent.mkdir(parents=True, exist_ok=True)
        partial.rename(args.output)
        metadata.update(status='completed', frames=frames, start_game_seconds=first_loop / LOOPS_PER_SECOND,
                        last_frame_game_seconds=last_loop / LOOPS_PER_SECOND, video_duration_seconds=frames / fps,
                        replay_game_seconds=info.game_duration_loops / LOOPS_PER_SECOND,
                        requested_end_game_seconds=end_loop / LOOPS_PER_SECOND,
                        termination='replay_result' if observation.player_result else 'segment_end',
                        game_version=info.game_version, map=info.map_name,
                        result=[dict(player=p.player_result.player_id, result=p.player_result.result) for p in info.player_info],
                        file_bytes=args.output.stat().st_size, camera_changes=sum(1 for _ in camera_path.open()),
                        wall_seconds=time.monotonic() - started)
        metadata['replay_unchanged'] = hashlib.sha256(args.replay.read_bytes()).hexdigest() == digest
        if not metadata['replay_unchanged']:
            raise RuntimeError('Source replay changed during rendering')
        plan_chapters = [(e['game_seconds'], f"Astra #{e['plan']['plan_id']}") for e in journal.events if e['event'] == 'plan_accepted']
        write_watch_page(args.output, metadata, sorted(chapters + plan_chapters))
        print(json.dumps(metadata, ensure_ascii=False, indent=2), flush=True)
    except BaseException as exc:
        metadata.update(status='failed', error=f'{type(exc).__name__}: {exc}', frames=frames)
        raise
    finally:
        try:
            if encoder is not None and encoder.poll() is None:
                if encoder.stdin and not encoder.stdin.closed:
                    try:
                        encoder.stdin.close()
                    except OSError:
                        pass
                try:
                    encoder.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    encoder.kill()
                    encoder.wait(timeout=10)
            decoder._safe_close()
        except Exception as cleanup_error:
            metadata['cleanup_error'] = f'{type(cleanup_error).__name__}: {cleanup_error}'
            logging.exception('Replay renderer cleanup failed')
        args.output.with_suffix('.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    main()
