"""Serve local replay reports and seekable MP4 files on localhost."""

import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import re


class ReplayHandler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header('Accept-Ranges', 'bytes')
        super().end_headers()

    def send_head(self):
        self.remaining = None
        header = self.headers.get('Range')
        path = Path(self.translate_path(self.path))
        if not header or not path.is_file():
            return super().send_head()
        source = path.open('rb')
        stat = os.fstat(source.fileno())
        if_range = self.headers.get('If-Range')
        if if_range and if_range != self.date_time_string(stat.st_mtime):
            source.close()
            return super().send_head()
        match = re.fullmatch(r'bytes=(\d*)-(\d*)', header.strip())
        start, end = 0, -1
        if match and any(match.groups()):
            first, last = match.groups()
            if first:
                start = int(first)
                end = min(int(last), stat.st_size - 1) if last else stat.st_size - 1
            elif int(last) > 0:
                start, end = max(0, stat.st_size - int(last)), stat.st_size - 1
        if end < start or start >= stat.st_size:
            source.close()
            self.send_response(416)
            self.send_header('Content-Range', f'bytes */{stat.st_size}')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return None
        self.remaining = end - start + 1
        source.seek(start)
        self.send_response(206)
        self.send_header('Content-Type', self.guess_type(str(path)))
        self.send_header('Content-Length', str(self.remaining))
        self.send_header('Content-Range', f'bytes {start}-{end}/{stat.st_size}')
        self.send_header('Last-Modified', self.date_time_string(stat.st_mtime))
        self.end_headers()
        return source

    def copyfile(self, source, destination):
        try:
            if self.remaining is None:
                return super().copyfile(source, destination)
            while self.remaining:
                data = source.read(min(self.remaining, 256 * 1024))
                if not data:
                    break
                destination.write(data)
                self.remaining -= len(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # Seeking cancels the previous HTTP response.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=Path.cwd())
    parser.add_argument('--port', type=int, default=8769)
    args = parser.parse_args()
    handler = partial(ReplayHandler, directory=str(args.directory.resolve(strict=True)))
    with ThreadingHTTPServer(('127.0.0.1', args.port), handler) as server:
        print(f'Replay viewer: http://127.0.0.1:{args.port}/ (Ctrl+C to stop)', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
