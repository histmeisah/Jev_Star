import http.client
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest

from sc2_rl_agent.starcraftenv_test.serve_replays import ReplayHandler


class ReplayServerTests(unittest.TestCase):
    def test_browser_byte_ranges_return_the_requested_segment(self):
        class QuietHandler(ReplayHandler):
            def log_message(self, *_):
                pass
        with tempfile.TemporaryDirectory() as tmp:
            original = bytes(range(100))
            (Path(tmp) / 'test.mp4').write_bytes(original)
            with ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietHandler, directory=tmp)) as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    for header, status, expected in [(None, 200, original), ('bytes=10-19', 206, original[10:20]),
                                                     ('bytes=-5', 206, original[-5:]), ('bytes=500-', 416, b'')]:
                        with self.subTest(header=header):
                            client = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
                            try:
                                client.request('GET', '/test.mp4', headers={'Range': header} if header else {})
                                response = client.getresponse()
                                self.assertEqual(response.status, status)
                                self.assertEqual(response.getheader('Accept-Ranges'), 'bytes')
                                self.assertEqual(response.read(), expected)
                                if header == 'bytes=10-19':
                                    self.assertEqual(response.getheader('Content-Range'), 'bytes 10-19/100')
                            finally:
                                client.close()
                finally:
                    server.shutdown()
                    thread.join(timeout=3)


if __name__ == '__main__':
    unittest.main()
