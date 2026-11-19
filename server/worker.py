"""Translation and punctuation in short-lived child processes.

The translator holds ~380 MB once used, and freeing its model inside the server gave
nothing back: the allocator keeps the pages (measured: 643 MB before and after unloading).
A process that exits does give them back. The page translates a video's sentences ahead
in batches, so the child is busy for the first seconds of a video and then goes away; the
server itself stays at the voice's baseline. Punctuation exits after each request so
its ONNX and PyTorch allocations are gone before the translator loads. Only one child
runs at a time, including when several tabs are dubbing.

  parent: Worker().call("translate", items=[[text, chars], ...])  ->  ["...", ...]
          Worker().call("punctuate", text="...")                   ->  ["...", ...]
  child:  python worker.py   (one JSON request per stdin line, one JSON reply per line)
"""

import json
import logging
import os
import subprocess
import sys
import threading
import time

IDLE_SECONDS = 20

log = logging.getLogger("youdub")


class Worker:
    def __init__(self):
        self.process = None
        self.operation = None
        self.lock = threading.Lock()  # one request at a time; the child is single-threaded
        self.last_used = 0.0
        threading.Thread(target=self._stop_when_idle, daemon=True).start()

    def call(self, op, **args):
        with self.lock:
            try:
                if self.process is None or self.operation != op or self.process.poll() is not None:
                    self._stop()
                    self.process = subprocess.Popen(
                        [sys.executable, __file__], stdin=subprocess.PIPE, stdout=subprocess.PIPE, encoding="utf-8"
                    )
                    self.operation = op
                    log.info("%s worker started (pid %d)", op, self.process.pid)
                self.process.stdin.write(json.dumps({"op": op, **args}) + "\n")
                self.process.stdin.flush()
                line = self.process.stdout.readline()
                if not line:
                    raise OSError(f"{op} worker exited")
                reply = json.loads(line)
            except (OSError, ValueError) as error:
                self._stop()
                raise RuntimeError(str(error)) from error
            finally:
                self.last_used = time.monotonic()
                if op == "punctuate":
                    self._stop()
        if "error" in reply:
            raise RuntimeError(reply["error"])
        return reply["result"]

    def _stop(self):
        if self.process is None:
            return
        process, operation = self.process, self.operation
        self.process = self.operation = None
        try:
            process.stdin.close()  # the child exits at end of input
        except BrokenPipeError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        finally:
            process.stdout.close()
        log.info("%s worker stopped", operation)

    def _stop_when_idle(self):
        while True:
            time.sleep(5)
            with self.lock:
                idle = time.monotonic() - self.last_used > IDLE_SECONDS
                if self.process and idle:
                    self._stop()


def main():
    protocol = sys.stdout
    sys.stdout = sys.stderr  # stray prints from libraries must not corrupt the replies
    os.nice(10)
    import punctuate
    import translate

    for line in sys.stdin:
        request = json.loads(line)
        try:
            if request["op"] == "translate":
                translate.load()
                result = translate.translate_many([(text, chars) for text, chars in request["items"]])
            else:
                result = punctuate.sentences(request["text"])
            reply = {"result": result}
        except Exception as error:  # report it; the server answers 500 and the page goes on
            reply = {"error": f"{type(error).__name__}: {error}"}
        protocol.write(json.dumps(reply, ensure_ascii=False) + "\n")
        protocol.flush()


if __name__ == "__main__":
    main()
