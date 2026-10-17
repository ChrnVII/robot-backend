"""Fixed inspection storage layout; never accepts a client supplied path."""
from datetime import datetime
from pathlib import Path
import re
from zoneinfo import ZoneInfo


def data_root(config):
    return Path(__file__).resolve().parents[3] / "data"


def task_directory(config, task_name, run_id):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', task_name).strip(' .')[:80] or '巡检'
    stamp = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y.%m.%d.%H%M%S.%f')
    return data_root(config) / 'cams' / f'{stamp}-{name}'
