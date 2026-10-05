"""请求体的 Pydantic 模型：字段缺失/非法时由全局异常处理器输出明确的 422 错误。"""
from __future__ import annotations

from datetime import date as date_type
from datetime import datetime
from typing import Any, Literal, Optional
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator, model_validator

SCAN_EVENT_TYPES = (
    "PICKED_UP",        # 揽收
    "IN_TRANSIT",       # 运输中
    "OUT_FOR_DELIVERY", # 派送中
    "DELIVERED",        # 签收
    "LOST_DECLARED",    # 承运方宣告遗失
    "DAMAGED_REPORTED", # 破损上报（payload.photo_evidence 标记是否已附照片）
)
EVENT_TYPES = SCAN_EVENT_TYPES + ("CORRECTION",)  # CORRECTION：迟到的更正事件


def _ensure_aware(v: datetime) -> datetime:
    if v.tzinfo is None:
        raise ValueError("时间必须携带时区偏移，例如 2026-09-28T10:00:00+08:00")
    return v


class OrderCreate(BaseModel):
    order_id: str = Field(min_length=1, max_length=64)
    created_at: datetime = Field(description="订单创建时间，须含时区偏移")
    timezone: str = Field(description="IANA 时区名，如 Asia/Shanghai")
    promise_type: Literal["hours", "business_days"]
    promise_value: int = Field(gt=0, le=3650, description="承诺时限：小时数或工作日数")
    holiday_calendar: str = Field(min_length=1, max_length=32, description="节假日表代码，如 CN")

    @field_validator("created_at")
    @classmethod
    def _created_at_aware(cls, v: datetime) -> datetime:
        return _ensure_aware(v)

    @field_validator("timezone")
    @classmethod
    def _valid_tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except Exception:
            raise ValueError(f"未知时区 {v!r}，需为 IANA 时区名，如 Asia/Shanghai")
        return v

    model_config = {"json_schema_extra": {"examples": [{
        "order_id": "ORD-2026-0001",
        "created_at": "2026-09-28T10:00:00+08:00",
        "timezone": "Asia/Shanghai",
        "promise_type": "business_days",
        "promise_value": 3,
        "holiday_calendar": "CN",
    }]}}


class EventIn(BaseModel):
    event_id: str = Field(min_length=1, max_length=64,
                          description="全局唯一事件 ID；重放同一 ID 不会重复计入")
    type: Literal["PICKED_UP", "IN_TRANSIT", "OUT_FOR_DELIVERY",
                  "DELIVERED", "LOST_DECLARED", "DAMAGED_REPORTED", "CORRECTION"]
    occurred_at: datetime = Field(description="事件发生时间；CORRECTION 为更正登记时间，须含时区")
    payload: dict[str, Any] = Field(default_factory=dict,
                                    description="事件负载，如 {\"photo_evidence\": true}")
    corrects_event_id: Optional[str] = Field(default=None, description="仅 CORRECTION：被更正事件 ID")
    corrected_occurred_at: Optional[datetime] = Field(default=None, description="仅 CORRECTION：更正后的发生时间")
    corrected_type: Optional[str] = Field(default=None, description="仅 CORRECTION：更正后的事件类型（可空）")

    @field_validator("occurred_at")
    @classmethod
    def _occurred_at_aware(cls, v: datetime) -> datetime:
        return _ensure_aware(v)

    @field_validator("corrected_occurred_at")
    @classmethod
    def _corrected_occurred_at_aware(cls, v: Optional[datetime]) -> Optional[datetime]:
        if v is not None:
            _ensure_aware(v)
        return v

    @model_validator(mode="after")
    def _check_correction_fields(self) -> "EventIn":
        if self.type == "CORRECTION":
            if not self.corrects_event_id:
                raise ValueError("CORRECTION 事件必须提供 corrects_event_id")
            if self.corrected_occurred_at is None:
                raise ValueError("CORRECTION 事件必须提供 corrected_occurred_at")
            if self.corrected_type is not None and self.corrected_type not in SCAN_EVENT_TYPES:
                raise ValueError(
                    f"corrected_type 须为 {list(SCAN_EVENT_TYPES)} 之一，不能更正为 CORRECTION")
        else:
            if self.corrects_event_id or self.corrected_occurred_at or self.corrected_type:
                raise ValueError(
                    "仅 CORRECTION 事件可携带 corrects_event_id / corrected_occurred_at / corrected_type")
        return self

    model_config = {"json_schema_extra": {"examples": [{
        "event_id": "EVT-0001",
        "type": "DELIVERED",
        "occurred_at": "2026-10-09T15:30:00+08:00",
        "payload": {"signed_by": "张三"},
    }]}}


class HolidayEntry(BaseModel):
    date: date_type = Field(description="节假日当地日期，YYYY-MM-DD")
    name: str = ""


class CalendarPut(BaseModel):
    holidays: list[HolidayEntry] = Field(description="整表替换该代码下的节假日")

    model_config = {"json_schema_extra": {"examples": [{
        "holidays": [{"date": "2026-10-01", "name": "国庆节"}],
    }]}}
