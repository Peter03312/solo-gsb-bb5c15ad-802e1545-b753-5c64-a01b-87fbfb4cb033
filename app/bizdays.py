"""工作日计算：截止日推算需跳过周末与当地节假日（按订单时区的当地日期判定）。"""
from __future__ import annotations

from datetime import date, datetime, timedelta


def is_business_day(d: date, holidays: set[date]) -> bool:
    return d.weekday() < 5 and d not in holidays


def add_business_days(
    start: datetime, n: int, holidays: set[date]
) -> tuple[datetime, list[str], list[str]]:
    """从 start 起向后数 n 个工作日（起始日当天不计入），保持时刻不变。

    返回 (结果时间, 跳过的周末日期列表, 跳过的节假日日期列表)，
    跳过明细用于判定依据的可解释性。
    """
    cur = start
    skipped_weekends: list[str] = []
    skipped_holidays: list[str] = []
    remaining = n
    while remaining > 0:
        cur += timedelta(days=1)
        while not is_business_day(cur.date(), holidays):
            if cur.date() in holidays:
                skipped_holidays.append(cur.date().isoformat())
            else:
                skipped_weekends.append(cur.date().isoformat())
            cur += timedelta(days=1)
        remaining -= 1
    return cur, skipped_weekends, skipped_holidays
