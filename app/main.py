"""快递理赔时限判定服务 — HTTP API。

运行：uvicorn app.main:app --host 0.0.0.0 --port 8000
配置：环境变量 CLAIMS_DB_PATH（默认 ./claims.db）
"""
from __future__ import annotations

import json
import os
from datetime import timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .bizdays import add_business_days
from .engine import compute_determination, effective_events, ingest_event
from .errors import ApiError, error_body
from .schemas import CalendarPut, EventIn, OrderCreate
from .seed import ensure_initialized
from .storage import Store
from .timeutil import iso_z, now_utc, parse_iso, to_utc

SERVICE_NAME = "快递理赔时限判定服务"
SERVICE_VERSION = "1.0.0"


def create_app(db_path: Optional[str] = None) -> FastAPI:
    app = FastAPI(
        title=SERVICE_NAME,
        version=SERVICE_VERSION,
        description="按订单创建时生效的政策版本判定理赔申请时限："
                    "工作日截止跳过当地节假日；事件幂等接入；更正事件只产生新判定。",
    )
    store = Store(db_path or os.environ.get("CLAIMS_DB_PATH", "claims.db"))
    ensure_initialized(store)
    app.state.store = store

    # ---------------- 依赖与错误处理 ----------------
    def get_store(request: Request) -> Store:
        return request.app.state.store

    @app.exception_handler(ApiError)
    async def api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(exc.code, exc.message, exc.details),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        missing, invalid = [], []
        for e in exc.errors():
            loc = ".".join(str(x) for x in e["loc"] if x not in ("body", "query", "path"))
            loc = loc or "(body)"
            if e["type"] == "missing":
                missing.append(loc)
            else:
                invalid.append({"field": loc, "message": e["msg"]})
        parts = []
        if missing:
            parts.append("缺少必填字段: " + ", ".join(missing))
        if invalid:
            parts.append("字段非法: " + "; ".join(f"{i['field']} ({i['message']})" for i in invalid))
        return JSONResponse(
            status_code=422,
            content=error_body(
                "VALIDATION_ERROR",
                "请求校验失败。" + "；".join(parts),
                {"missing": missing, "invalid": invalid},
            ),
        )

    # ---------------- 基础 ----------------
    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok", "service": SERVICE_NAME, "version": SERVICE_VERSION}

    @app.get("/")
    def index() -> dict:
        return {
            "service": SERVICE_NAME,
            "version": SERVICE_VERSION,
            "docs": "/docs",
            "endpoints": [
                "GET  /v1/policies",
                "PUT  /v1/holiday-calendars/{code}",
                "GET  /v1/holiday-calendars/{code}",
                "POST /v1/orders",
                "GET  /v1/orders/{order_id}",
                "POST /v1/orders/{order_id}/events",
                "GET  /v1/orders/{order_id}/events",
                "GET  /v1/orders/{order_id}/determination",
                "GET  /v1/orders/{order_id}/determinations",
            ],
        }

    # ---------------- 政策与节假日 ----------------
    @app.get("/v1/policies")
    def list_policies(store: Store = Depends(get_store)) -> dict:
        return {"policies": store.list_policies()}

    @app.put("/v1/holiday-calendars/{code}")
    def put_calendar(code: str, body: CalendarPut, store: Store = Depends(get_store)) -> dict:
        holidays = [{"date": h.date.isoformat(), "name": h.name} for h in body.holidays]
        store.replace_calendar(code, holidays)
        return {"code": code, "count": len(holidays)}

    @app.get("/v1/holiday-calendars/{code}")
    def get_calendar(code: str, store: Store = Depends(get_store)) -> dict:
        if not store.calendar_exists(code):
            raise ApiError(404, "CALENDAR_NOT_FOUND", f"节假日表 {code!r} 不存在。",
                           {"available": store.list_calendars()})
        return {"code": code, "holidays": store.list_holiday_entries(code)}

    # ---------------- 订单 ----------------
    @app.post("/v1/orders", status_code=201)
    def create_order(body: OrderCreate, store: Store = Depends(get_store)) -> dict:
        if store.get_order(body.order_id):
            raise ApiError(409, "ORDER_EXISTS", f"订单 {body.order_id!r} 已存在。",
                           {"order_id": body.order_id})
        if not store.calendar_exists(body.holiday_calendar):
            raise ApiError(422, "UNKNOWN_CALENDAR",
                           f"节假日表 {body.holiday_calendar!r} 不存在，请先创建。",
                           {"available": store.list_calendars()})

        created_utc = to_utc(body.created_at)
        tz = ZoneInfo(body.timezone)
        created_local = created_utc.astimezone(tz)

        # 规则按订单创建时（当地日期）生效的政策版本计算，创建时固化
        policy = store.resolve_policy(created_local.date())
        if policy is None:
            raise ApiError(422, "NO_EFFECTIVE_POLICY",
                           "订单创建日期早于任何政策版本的生效日期。",
                           {"created_local_date": created_local.date().isoformat()})

        holidays = store.get_holidays(body.holiday_calendar)
        if body.promise_type == "hours":
            promised_by = created_utc + timedelta(hours=body.promise_value)
            promise_skips = {"weekends_skipped": [], "holidays_skipped": []}
        else:
            promised_local, wk, hd = add_business_days(created_local, body.promise_value, holidays)
            promised_by = promised_local.astimezone(timezone.utc)
            promise_skips = {"weekends_skipped": wk, "holidays_skipped": hd}

        store.insert_order({
            "order_id": body.order_id,
            "created_at": iso_z(created_utc),
            "timezone": body.timezone,
            "promise_type": body.promise_type,
            "promise_value": body.promise_value,
            "holiday_calendar": body.holiday_calendar,
            "policy_version": policy["version"],
            "promised_by": iso_z(promised_by),
            "recorded_at": iso_z(now_utc()),
        })
        return _order_view(store, body.order_id) | {"promise_basis": promise_skips}

    @app.get("/v1/orders/{order_id}")
    def get_order(order_id: str, store: Store = Depends(get_store)) -> dict:
        return _order_view(store, order_id)

    # ---------------- 事件 ----------------
    @app.post("/v1/orders/{order_id}/events", status_code=201)
    def post_event(order_id: str, body: EventIn, store: Store = Depends(get_store)) -> dict:
        order = _must_get_order(store, order_id)
        return ingest_event(store, order, body)

    @app.get("/v1/orders/{order_id}/events")
    def list_events(order_id: str, store: Store = Depends(get_store)) -> dict:
        _must_get_order(store, order_id)
        rows = store.list_events(order_id)
        eff, applied = effective_events(rows)
        return {
            "order_id": order_id,
            "raw_events": [
                {
                    "event_id": r["event_id"],
                    "seq": r["seq"],
                    "type": r["type"],
                    "occurred_at": r["occurred_at"],
                    "payload": json.loads(r["payload_json"]),
                    "corrects_event_id": r["corrects_event_id"],
                    "corrected_occurred_at": r["corrected_occurred_at"],
                    "corrected_type": r["corrected_type"],
                    "received_at": r["received_at"],
                }
                for r in rows
            ],
            "effective_timeline": [
                {
                    "event_id": e["event_id"],
                    "type": e["type"],
                    "occurred_at": iso_z(e["occurred_at"]),
                    "corrected_by": e["corrected_by"],
                }
                for e in eff
            ],
            "corrections_applied": applied,
        }

    # ---------------- 判定 ----------------
    @app.get("/v1/orders/{order_id}/determination")
    def get_determination(
        order_id: str,
        as_of: Optional[str] = Query(default=None,
                                     description="判定基准时间（ISO-8601），默认当前时间"),
        store: Store = Depends(get_store),
    ) -> dict:
        order = _must_get_order(store, order_id)
        if as_of is None:
            as_of_dt = now_utc()
        else:
            try:
                as_of_dt = parse_iso(as_of)
            except ValueError as exc:
                raise ApiError(422, "INVALID_AS_OF", str(exc), {"as_of": as_of})
        result = compute_determination(
            order,
            store.list_events(order_id),
            store.get_holidays(order["holiday_calendar"]),
            store.get_policy(order["policy_version"]),
            as_of_dt,
        )
        result["determination_version"] = store.count_events(order_id)
        return result

    @app.get("/v1/orders/{order_id}/determinations")
    def list_determinations(order_id: str, store: Store = Depends(get_store)) -> dict:
        _must_get_order(store, order_id)
        rows = store.list_determinations(order_id)
        current = rows[-1]["version"] if rows else 0
        return {
            "order_id": order_id,
            "current_version": current,
            "history": [
                {
                    "version": r["version"],
                    "archived": r["version"] != current,  # 归档判定只读，永不改写
                    "recorded_at": r["recorded_at"],
                    "determination": json.loads(r["result_json"]),
                }
                for r in rows
            ],
        }

    # ---------------- 内部辅助 ----------------
    def _must_get_order(store: Store, order_id: str) -> dict:
        order = store.get_order(order_id)
        if order is None:
            raise ApiError(404, "ORDER_NOT_FOUND", f"订单 {order_id!r} 不存在。",
                           {"order_id": order_id})
        return order

    def _order_view(store: Store, order_id: str) -> dict:
        order = _must_get_order(store, order_id)
        return {
            "order_id": order["order_id"],
            "created_at": order["created_at"],
            "timezone": order["timezone"],
            "promise_type": order["promise_type"],
            "promise_value": order["promise_value"],
            "holiday_calendar": order["holiday_calendar"],
            "policy_version": order["policy_version"],
            "promised_by": order["promised_by"],
            "events_count": store.count_events(order_id),
        }

    return app


app = create_app()
