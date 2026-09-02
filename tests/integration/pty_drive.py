#!/usr/bin/env python3
"""Drive an interactive command through a real pty, answering prompts in order.

usage: pty_drive.py <transcript-file> <prompt-substring>=<answer> ... -- <command...>
Each prompt is matched once, in order, against the accumulated terminal output.
The transcript (everything the command printed) is written to the given file.
"""
import os
import pty
import select
import subprocess
import sys
import time


def main() -> int:
    transcript_path = sys.argv[1]
    split = sys.argv.index("--")
    answers = [a.split("=", 1) for a in sys.argv[2:split]]
    command = sys.argv[split + 1 :]
    master, slave = pty.openpty()
    process = subprocess.Popen(
        command, stdin=slave, stdout=slave, stderr=slave, close_fds=True
    )
    os.close(slave)
    buffer = b""
    transcript = open(transcript_path, "wb")
    pending = list(answers)
    deadline = time.time() + 900
    while True:
        if process.poll() is not None and not select.select([master], [], [], 0.2)[0]:
            break
        if time.time() > deadline:
            process.kill()
            break
        ready, _, _ = select.select([master], [], [], 0.5)
        if ready:
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            transcript.write(chunk)
            transcript.flush()
            buffer += chunk
        if pending and pending[0][0].encode() in buffer:
            prompt, answer = pending.pop(0)
            buffer = buffer.split(prompt.encode(), 1)[1]
            os.write(master, (answer + "\n").encode())
    transcript.close()
    return process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
