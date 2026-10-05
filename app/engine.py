"""理赔时限判定引擎。

判定语义（详见 README）：
- 政策版本按订单创建日（订单时区当地日期）生效版本确定，创建时固化到订单上。
- 触发点（理赔窗口起点）按优先级取：LOST_DECLARED > DAMAGED_REPORTED > DELIVERED > 承诺违约点。
- 截止时间 = 触发点 + 政策窗口（自然日直接加；工作日跳过周末与当地节假日）。
- 同一 event_id 重放不重复计入；CORRECTION 事件只追加新判定版本，已归档判定不改写。
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .bizdays import add_business_days
from .errors import ApiError
from .schemas import EventIn
from .storage import Store, payload_fingerprint
from .timeutil import iso_z, now_utc, parse_iso, to_utc

REASON_TEXT = {
    "OK": "在理赔申请时限内且证据齐全，可申请理赔。",
    "NO_BREACH": "未发生可理赔的违约情形（承诺未逾期，或已按时送达）。",
    "MISSING_EVIDENCE": "存在缺失证据，补齐后方可申请理赔。",
    "WINDOW_NOT_OPEN": "理赔窗口尚未开启（触发点未到）。",
    "WINDOW_CLOSED": "已超过理赔申请截止时间。",
}


# ---------------------------------------------------------------- 事件时间线

def effective_events(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """把原始事件流水折叠为有效时间线：CORRECTION 事件就地替换其目标事件的
    发生时间/类型（原事件行本身不可变，更正以新事件行追加）。

    返回 (有效事件列表, 已应用的更正列表)。
    """
    corrections = {r["corrects_event_id"]: r for r in rows if r["type"] == "CORRECTION"}
    eff: list[dict[str, Any]] = []
    applied: list[dict[str, str]] = []
    for r in rows:
        if r["type"] == "CORRECTION":
            continue
        c = corrections.get(r["event_id"])
        if c:
            applied.append({
                "corrects_event_id": r["event_id"],
                "correction_event_id": c["event_id"],
                "original_occurred_at": r["occurred_at"],
                "corrected_occurred_at": c["corrected_occurred_at"],
            })
            eff.append({
                "event_id": r["event_id"],
                "type": c["corrected_type"] or r["type"],
                "occurred_at": parse_iso(c["corrected_occurred_at"]),
                "payload": json.loads(r["payload_json"]),
                "seq": r["seq"],
                "corrected_by": c["event_id"],
            })
        else:
            eff.append({
                "event_id": r["event_id"],
                "type": r["type"],
                "occurred_at": parse_iso(r["occurred_at"]),
                "payload": json.loads(r["payload_json"]),
                "seq": r["seq"],
                "corrected_by": None,
            })
    eff.sort(key=lambda e: (e["occurred_at"], e["seq"]))
    return eff, applied


# ---------------------------------------------------------------- 判定计算

def compute_determination(
    order: dict[str, Any],
    event_rows: list[dict[str, Any]],
    holidays: set[date],
    policy: dict[str, Any],
    as_of: datetime,
) -> dict[str, Any]:
    tz = ZoneInfo(order["timezone"])
    promised_by = parse_iso(order["promised_by"])
    as_of = to_utc(as_of)

    eff, applied_corrections = effective_events(event_rows)
    last_by_type: dict[str, dict[str, Any]] = {}
    for e in eff:
        last_by_type[e["type"]] = e
    lost = last_by_type.get("LOST_DECLARED")
    damaged = last_by_type.get("DAMAGED_REPORTED")
    delivered = last_by_type.get("DELIVERED")

    # ---- 违约情形与触发点 ----
    trigger_event_id = None
    if lost:
        breach, trigger_kind, trigger_time = "LOST", "LOST_DECLARED", lost["occurred_at"]
        trigger_event_id = lost["event_id"]
    elif damaged:
        breach, trigger_kind, trigger_time = "DAMAGED", "DAMAGED_REPORTED", damaged["occurred_at"]
        trigger_event_id = damaged["event_id"]
    elif delivered:
        breach = "LATE_DELIVERY" if delivered["occurred_at"] > promised_by else "NONE"
        trigger_kind, trigger_time = "DELIVERED", delivered["occurred_at"]
        trigger_event_id = delivered["event_id"]
    else:
        breach = "NON_DELIVERY" if as_of > promised_by else "NONE"
        trigger_kind, trigger_time = "PROMISE_BREACH", promised_by

    # ---- 理赔窗口 ----
    window_open_at = trigger_time
    if policy["claim_window_type"] == "calendar_days":
        deadline = trigger_time + timedelta(days=int(policy["claim_window_value"]))
        skipped_weekends: list[str] = []
        skipped_holidays: list[str] = []
    else:  # business_days：按订单时区当地日期跳过周末与节假日
        local_trigger = trigger_time.astimezone(tz)
        local_deadline, skipped_weekends, skipped_holidays = add_business_days(
            local_trigger, int(policy["claim_window_value"]), holidays)
        deadline = local_deadline.astimezone(timezone.utc)

    # ---- 缺失证据 ----
    missing: list[str] = []
    if breach == "NON_DELIVERY":
        missing.append("lost_declaration")  # 需承运方宣告遗失方可提起未送达理赔
    if breach == "DAMAGED" and policy["damage_photo_required"]:
        if not (damaged and damaged["payload"].get("photo_evidence")):
            missing.append("damage_photo")  # 当前政策要求破损理赔附照片证据

    # ---- 结论 ----
    if breach == "NONE":
        can_apply, reason_code = False, "NO_BREACH"
    elif missing:
        can_apply, reason_code = False, "MISSING_EVIDENCE"
    elif as_of < window_open_at:
        can_apply, reason_code = False, "WINDOW_NOT_OPEN"
    elif as_of > deadline:
        can_apply, reason_code = False, "WINDOW_CLOSED"
    else:
        can_apply, reason_code = True, "OK"

    return {
        "order_id": order["order_id"],
        "as_of": iso_z(as_of),
        "can_apply_now": can_apply,
        "reason_code": reason_code,
        "reason": REASON_TEXT[reason_code],
        "breach": breach,
        "window_open_at": iso_z(window_open_at),
        "deadline": iso_z(deadline),
        "missing_evidence": missing,
        "basis": {
            "policy_version": policy["version"],
            "policy_effective_from": policy["effective_from"],
            "policy_description": policy["description"],
            "claim_window": {
                "type": policy["claim_window_type"],
                "value": policy["claim_window_value"],
            },
            "timezone": order["timezone"],
            "holiday_calendar": order["holiday_calendar"],
            "promised_by": iso_z(promised_by),
            "trigger": {
                "kind": trigger_kind,
                "event_id": trigger_event_id,
                "occurred_at": iso_z(trigger_time),
            },
            "weekends_skipped": skipped_weekends,
            "holidays_skipped": skipped_holidays,
            "events_considered": len(eff),
            "corrections_applied": applied_corrections,
        },
    }


# ---------------------------------------------------------------- 事件接入

def ingest_event(store: Store, order: dict[str, Any], ev: EventIn) -> dict[str, Any]:
    """接入一个扫描事件：幂等、保序、更正只追加新判定。"""
    order_id = order["order_id"]
    occurred_utc = to_utc(ev.occurred_at)
    fingerprint = payload_fingerprint(
        order_id, ev.type, iso_z(occurred_utc), ev.payload,
        ev.corrects_event_id,
        iso_z(to_utc(ev.corrected_occurred_at)) if ev.corrected_occurred_at else None,
        ev.corrected_type,
    )

    with store._lock:  # 同一订单的接入串行化
        existing = store.get_event(ev.event_id)
        if existing is not None:
            if existing["order_id"] != order_id or existing["payload_hash"] != fingerprint:
                raise ApiError(
                    409, "EVENT_ID_CONFLICT",
                    f"event_id {ev.event_id!r} 已存在且内容不一致，拒绝写入。",
                    {"event_id": ev.event_id})
            # 幂等重放：不重复计入，不产生新判定版本
            return {
                "applied": False,
                "duplicate": True,
                "event_id": ev.event_id,
                "determination_version": store.count_events(order_id),
            }

        rows = store.list_events(order_id)
        if rows:
            max_occurred = max(parse_iso(r["occurred_at"]) for r in rows)
            if occurred_utc < max_occurred:
                raise ApiError(
                    422, "EVENT_OUT_OF_ORDER",
                    "事件时间倒序：本事件 occurred_at 早于该订单已接收的最大事件时间。"
                    "如需修正历史事件，请使用 CORRECTION 类型事件。",
                    {
                        "event_id": ev.event_id,
                        "occurred_at": iso_z(occurred_utc),
                        "max_occurred_at": iso_z(max_occurred),
                    })

        if ev.type == "CORRECTION":
            target = store.get_event(ev.corrects_event_id)
            if target is None or target["order_id"] != order_id:
                raise ApiError(
                    422, "CORRECTION_TARGET_NOT_FOUND",
                    f"被更正事件 {ev.corrects_event_id!r} 在该订单下不存在。",
                    {"corrects_event_id": ev.corrects_event_id})
            if target["type"] == "CORRECTION":
                raise ApiError(
                    422, "CANNOT_CORRECT_CORRECTION",
                    "不能对 CORRECTION 事件再做更正。",
                    {"corrects_event_id": ev.corrects_event_id})
            if store.has_correction_for(ev.corrects_event_id):
                raise ApiError(
                    409, "TARGET_ALREADY_CORRECTED",
                    f"事件 {ev.corrects_event_id!r} 已被更正过，不允许重复更正。",
                    {"corrects_event_id": ev.corrects_event_id})

        store.insert_event({
            "event_id": ev.event_id,
            "order_id": order_id,
            "seq": len(rows) + 1,
            "type": ev.type,
            "occurred_at": iso_z(occurred_utc),
            "payload_json": json.dumps(ev.payload, ensure_ascii=False, sort_keys=True),
            "corrects_event_id": ev.corrects_event_id,
            "corrected_occurred_at": (
                iso_z(to_utc(ev.corrected_occurred_at)) if ev.corrected_occurred_at else None),
            "corrected_type": ev.corrected_type,
            "payload_hash": fingerprint,
            "received_at": iso_z(now_utc()),
        })

        # 每个被应用的事件产生一个新的判定快照；历史快照保持原样（归档不可改写）
        rows = store.list_events(order_id)
        holidays = store.get_holidays(order["holiday_calendar"])
        policy = store.get_policy(order["policy_version"])
        snapshot = compute_determination(order, rows, holidays, policy, now_utc())
        version = store.count_events(order_id)
        snapshot["determination_version"] = version
        store.insert_determination(order_id, version, snapshot)

        return {
            "applied": True,
            "duplicate": False,
            "event_id": ev.event_id,
            "determination_version": version,
        }
