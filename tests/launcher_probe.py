"""Child payload for launcher tests: records the real interpreter/console and stays alive."""
import ctypes
import json
import os
from pathlib import Path
import sys
import time

folder = Path(__file__).resolve().parent
kernel = ctypes.WinDLL('kernel32')
kernel.GetConsoleWindow.restype = ctypes.c_void_p
for tick in range(300):
    if (folder / 'stop-probe').exists():
        break
    result = {'pid': os.getpid(), 'executable': sys.executable, 'prefix': sys.prefix,
              'console': kernel.GetConsoleWindow() or 0, 'tick': tick, 'args': sys.argv[1:]}
    pending = folder / 'probe.tmp'
    pending.write_text(json.dumps(result), encoding='utf-8')
    pending.replace(folder / 'probe.json')
    time.sleep(0.1)
