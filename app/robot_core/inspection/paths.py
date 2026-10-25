"""Fixed inspection storage layout; never accepts a client supplied path.

每个任务一个目录，任务下每个摄像头一个子目录::

    data/cams/<日期时间-任务名>/
        cam01/video.mkv          该路主码流原始转封装
        cam01/inspection.json    该路的检测记录
        cam02/...
"""

from datetime import datetime
from pathlib import Path
import re
from zoneinfo import ZoneInfo

_CAMERA_ID = re.compile(r'[A-Za-z0-9._-]{1,64}')


def data_root(config):
    return Path(__file__).resolve().parents[3] / "data"


def task_directory(config, task_name, run_id):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', task_name).strip(' .')[:80] or '巡检'
    stamp = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y.%m.%d.%H%M%S.%f')
    return data_root(config) / 'cams' / f'{stamp}-{name}'


def camera_directory(directory, camera_id):
    """任务目录下该路的子目录；camera_id 直接进路径，必须先校验。"""
    value = str(camera_id)
    if not _CAMERA_ID.fullmatch(value):
        raise ValueError(f'invalid camera id for storage path: {value!r}')
    return Path(directory) / value


def camera_video_path(directory, camera_id):
    return camera_directory(directory, camera_id) / 'video.mkv'


def camera_records_path(directory, camera_id):
    return camera_directory(directory, camera_id) / 'inspection.json'


def camera_journal_path(directory, camera_id):
    return camera_directory(directory, camera_id) / '.inspection.records'
