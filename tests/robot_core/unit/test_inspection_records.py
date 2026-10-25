"""分页读取检测记录：内存与文件大小无关，且两种存储格式都能读。"""
import json

import pytest

from app.robot_core.inspection import records

pytestmark = pytest.mark.no_database


def _journal(tmp_path, count, *, broken_tail=False):
    path = tmp_path / '.inspection.records'
    with path.open('w', encoding='utf-8') as handle:
        for index in range(count):
            handle.write(json.dumps({'frame_id': index, 'camera_id': 'cam01'}) + '\n')
        if broken_tail:
            handle.write('{"frame_id": 999, "camera')       # 进程被杀留下的半行
    return path


def _document(tmp_path, count):
    path = tmp_path / 'inspection.json'
    path.write_text(json.dumps({
        'metadata': {'task_run_id': 'run', 'records_total': count},
        'records_total': count,
        'records_complete': True,
        'records': [{'frame_id': index} for index in range(count)],
    }, ensure_ascii=False), encoding='utf-8')
    return path


def test_pages_streaming_journal(tmp_path):
    path = _journal(tmp_path, 500)
    items, total = records.page(path, offset=100, limit=3)
    assert total == 500
    assert [item['frame_id'] for item in items] == [100, 101, 102]

    # 第二页命中「文件未变」的缓存总数，提前结束扫描但总数仍然正确
    items, total = records.page(path, offset=499, limit=200)
    assert total == 500
    assert [item['frame_id'] for item in items] == [499]


def test_page_past_end_is_empty_but_keeps_total(tmp_path):
    path = _journal(tmp_path, 10)
    items, total = records.page(path, offset=10, limit=5)
    assert items == []
    assert total == 10


def test_broken_tail_line_is_skipped(tmp_path):
    path = _journal(tmp_path, 3, broken_tail=True)
    items, total = records.page(path, offset=0, limit=10)
    assert total == 3
    assert [item['frame_id'] for item in items] == [0, 1, 2]


def test_pages_complete_document_array(tmp_path):
    path = _document(tmp_path, 50)
    items, total = records.page(path, offset=10, limit=2)
    assert total == 50
    assert items == [{'frame_id': 10}, {'frame_id': 11}]


def test_large_document_does_not_depend_on_chunk_size(tmp_path):
    """记录比读块小、文件比读块大：跨块解析不能漏条或重复。"""
    count = records.CHUNK // 8
    path = _document(tmp_path, count)
    items, total = records.page(path, offset=count - 2, limit=2)
    assert total == count
    assert [item['frame_id'] for item in items] == [count - 2, count - 1]


def test_missing_file_is_an_empty_page(tmp_path):
    assert records.page(tmp_path / 'nope.json', 0, 10) == ([], 0)
