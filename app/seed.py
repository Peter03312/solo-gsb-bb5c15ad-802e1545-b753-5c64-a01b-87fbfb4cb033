"""初始化种子数据：政策版本与示例节假日表。幂等，可重复执行。

注意：节假日为演示用示例数据，生产环境应通过 PUT /v1/holiday-calendars/{code}
维护权威节假日表。
"""
from __future__ import annotations

from .storage import Store

DEFAULT_POLICIES = [
    {
        "version": "POLICY-2024",
        "effective_from": "2024-01-01",
        "claim_window_type": "calendar_days",
        "claim_window_value": 30,
        "damage_photo_required": 0,
        "description": "2024 版：触发点后 30 个自然日内可申请理赔。",
    },
    {
        "version": "POLICY-2025",
        "effective_from": "2025-01-01",
        "claim_window_type": "business_days",
        "claim_window_value": 15,
        "damage_photo_required": 1,
        "description": "2025 版：触发点后 15 个工作日内可申请（跳过当地节假日）；"
                       "破损理赔须附照片证据。",
    },
]

_CN_2026 = [
    ("2026-01-01", "元旦"),
    ("2026-02-16", "春节(示例)"), ("2026-02-17", "春节(示例)"), ("2026-02-18", "春节(示例)"),
    ("2026-02-19", "春节(示例)"), ("2026-02-20", "春节(示例)"), ("2026-02-21", "春节(示例)"),
    ("2026-02-22", "春节(示例)"),
    ("2026-04-04", "清明节(示例)"), ("2026-04-05", "清明节(示例)"), ("2026-04-06", "清明节(示例)"),
    ("2026-05-01", "劳动节(示例)"), ("2026-05-02", "劳动节(示例)"), ("2026-05-03", "劳动节(示例)"),
    ("2026-05-04", "劳动节(示例)"), ("2026-05-05", "劳动节(示例)"),
    ("2026-06-19", "端午节(示例)"),
    ("2026-09-25", "中秋节(示例)"),
    ("2026-10-01", "国庆节(示例)"), ("2026-10-02", "国庆节(示例)"), ("2026-10-03", "国庆节(示例)"),
    ("2026-10-04", "国庆节(示例)"), ("2026-10-05", "国庆节(示例)"), ("2026-10-06", "国庆节(示例)"),
    ("2026-10-07", "国庆节(示例)"),
]

_US_2026 = [
    ("2026-01-01", "New Year's Day"),
    ("2026-05-25", "Memorial Day"),
    ("2026-07-03", "Independence Day (observed)"),
    ("2026-09-07", "Labor Day"),
    ("2026-11-26", "Thanksgiving"),
    ("2026-12-25", "Christmas Day"),
]

DEFAULT_CALENDARS = {"CN": _CN_2026, "US": _US_2026}


def ensure_initialized(store: Store) -> None:
    """建表 + 种子数据，幂等。"""
    store.init_schema()
    for p in DEFAULT_POLICIES:
        store.upsert_policy(p)
    for code, days in DEFAULT_CALENDARS.items():
        store.ensure_calendar(code)
        for day, name in days:
            store.add_holiday(code, day, name)
