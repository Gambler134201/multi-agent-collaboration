"""Start the hidden panel without inheriting the caller's terminal/pipeline handles."""
import argparse
import json
from pathlib import Path
import subprocess
import sys


def start(port=8765):
    if not 1 <= port <= 65535:
        raise ValueError('invalid panel port')
    here = Path(__file__).resolve().parent
    python = here / 'venv/Scripts/python.exe'
    with (here / 'panel.stdout.log').open('ab', buffering=0) as stdout, (here / 'panel.stderr.log').open('ab', buffering=0) as stderr:
        process = subprocess.Popen([str(python), str(here / 'panel_server.py'), '--port', str(port)],
            cwd=here.parent, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, close_fds=True,
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == 'win32' else 0)
    return process.pid


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    print(json.dumps({'pid': start(args.port)}))
