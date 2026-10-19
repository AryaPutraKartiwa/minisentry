"""Validation of incoming events. Unknown fields are kept (``extra="allow"``)
so newer SDKs can send more data without breaking older servers."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Frame(BaseModel):
    model_config = ConfigDict(extra="allow")

    filename: str | None = None
    abs_path: str | None = None
    module: str | None = None
    function: str | None = None
    lineno: int | None = None
    in_app: bool = False


class Stacktrace(BaseModel):
    frames: list[Frame] = Field(default_factory=list, max_length=250)


class ExceptionValue(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str = Field(max_length=256)
    value: str = Field(default="", max_length=8192)
    module: str | None = None
    stacktrace: Stacktrace | None = None


class ExceptionInterface(BaseModel):
    values: list[ExceptionValue] = Field(min_length=1, max_length=10)


class Event(BaseModel):
    model_config = ConfigDict(extra="allow")

    event_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    timestamp: datetime
    level: Literal["debug", "info", "warning", "error", "fatal"] = "error"
    platform: str = "python"
    message: str | None = Field(default=None, max_length=8192)
    exception: ExceptionInterface | None = None
    environment: str | None = None
    release: str | None = None
    server_name: str | None = None
    fingerprint: list[str] | None = Field(default=None, max_length=20)
    tags: dict[str, str] = Field(default_factory=dict)
