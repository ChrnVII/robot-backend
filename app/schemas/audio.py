"""独立音频录制的出入参（camelCase 线上契约）。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class StartAudioRecordingRequest(BaseModel):
    """开始一段录音：默认写进给定任务目录（`<directory>/audio/session.wav`）。

    有视频时由调用方传入 `POST /inspections` 建好的目录，音频与视频落在同一处，
    前端只显示一个保存路径；**只录声音**时不传 `directory`，后端在 `data/cams` 下
    自己新建一个采集目录（命名与巡检任务一致，便于按日期清理）。
    """

    directory: str | None = Field(
        default=None,
        max_length=1024,
        description="任务目录绝对路径（须在 data/cams 下）；省略则自动新建",
    )
