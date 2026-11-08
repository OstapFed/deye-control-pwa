"""Background launcher for Windows autostart (pythonw.exe has no console).

Usage: pythonw.exe run_service.py <data-folder>
Writes errors to <data-folder>/server.log and restarts the gateway if it crashes.
"""
import os, sys, time, traceback, runpy
from pathlib import Path

if len(sys.argv) > 1: os.environ['DEYE_DATA'] = sys.argv[1]
data = Path(os.environ.get('DEYE_DATA', str(Path(__file__).with_name('data'))))
data.mkdir(parents=True, exist_ok=True)
log_path = data / 'server.log'
if log_path.exists() and log_path.stat().st_size > 2_000_000:
    log_path.replace(data / 'server.old.log')
log = open(log_path, 'a', encoding='utf-8', buffering=1)
sys.stdout = sys.stderr = log
os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
sys.path.insert(0, str(Path(__file__).parent))

while True:
    print(time.strftime('%Y-%m-%d %H:%M:%S'), 'DEYE gateway starting', flush=True)
    try:
        runpy.run_path(str(Path(__file__).with_name('app.py')), run_name='__main__')
    except SystemExit as e:
        print('Gateway exited:', e, flush=True)
        if str(e).startswith('Задайте'): break
    except Exception:
        traceback.print_exc()
    time.sleep(10)
