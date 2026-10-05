#!/usr/bin/env python3
"""端到端调用示例：演示快递理赔时限判定服务的完整流程。

前置：服务已在 BASE_URL（默认 http://127.0.0.1:8000）运行。
运行：python examples/demo_client.py

场景：2026-09-28 创建的订单，承诺 3 个工作日送达（跨国庆黄金周），
实际 2026-10-09 才签收（迟延），演示判定、幂等重放、乱序拒绝、迟到更正。
"""
import os
import sys

import httpx

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8000")
ORDER_ID = os.environ.get("DEMO_ORDER_ID", "ORD-DEMO-0001")

client = httpx.Client(base_url=BASE, timeout=10.0)


def step(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def show(resp: httpx.Response) -> dict:
    print(f"{resp.request.method} {resp.request.url.path}"
          f"{'?' + resp.request.url.query.decode() if resp.request.url.query else ''}"
          f" -> {resp.status_code}")
    body = resp.json()
    import json
    print(json.dumps(body, ensure_ascii=False, indent=2)[:2000])
    return body


def main() -> None:
    step("0. 健康检查")
    show(client.get("/healthz"))

    step("1. 查看政策版本（按订单创建日生效）")
    show(client.get("/v1/policies"))

    step("2. 创建订单：2026-09-28 10:00(+08:00)，承诺 3 个工作日，CN 节假日表")
    resp = client.post("/v1/orders", json={
        "order_id": ORDER_ID,
        "created_at": "2026-09-28T10:00:00+08:00",
        "timezone": "Asia/Shanghai",
        "promise_type": "business_days",
        "promise_value": 3,
        "holiday_calendar": "CN",
    })
    if resp.status_code == 409:  # 重复演示时订单已存在，继续后续步骤
        print("订单已存在，跳过创建（409）")
    else:
        resp.raise_for_status()
        order = show(resp)
        # 9/29、9/30 为工作日，10/1-10/7 国庆节假日跳过 -> 承诺截止 10/8
        print(f">>> 承诺送达截止 promised_by = {order['promised_by']}（已跳过国庆节假日）")

    step("3. 推送扫描事件：揽收 -> 运输中")
    show(client.post(f"/v1/orders/{ORDER_ID}/events", json={
        "event_id": "EVT-DEMO-1", "type": "PICKED_UP",
        "occurred_at": "2026-09-28T14:00:00+08:00", "payload": {"site": "上海浦东"},
    }))
    show(client.post(f"/v1/orders/{ORDER_ID}/events", json={
        "event_id": "EVT-DEMO-2", "type": "IN_TRANSIT",
        "occurred_at": "2026-09-30T09:00:00+08:00", "payload": {"city": "武汉"},
    }))

    step("4. 中途查询（as_of=2026-10-10，已逾承诺期但未签收）：缺失证据 lost_declaration")
    show(client.get(f"/v1/orders/{ORDER_ID}/determination",
                    params={"as_of": "2026-10-10T10:00:00+08:00"}))

    step("5. 继续推送：派送中 -> 签收（2026-10-09，迟于承诺 10-08）")
    show(client.post(f"/v1/orders/{ORDER_ID}/events", json={
        "event_id": "EVT-DEMO-3", "type": "OUT_FOR_DELIVERY",
        "occurred_at": "2026-10-09T09:00:00+08:00", "payload": {},
    }))
    show(client.post(f"/v1/orders/{ORDER_ID}/events", json={
        "event_id": "EVT-DEMO-4", "type": "DELIVERED",
        "occurred_at": "2026-10-09T15:30:00+08:00", "payload": {"signed_by": "张三"},
    }))

    step("6. 查询判定：迟延送达，窗口开启（POLICY-2025，15 个工作日，跳过周末）")
    show(client.get(f"/v1/orders/{ORDER_ID}/determination",
                    params={"as_of": "2026-10-12T10:00:00+08:00"}))

    step("7. 幂等重放：重复推送 EVT-DEMO-4，不会重复计入")
    show(client.post(f"/v1/orders/{ORDER_ID}/events", json={
        "event_id": "EVT-DEMO-4", "type": "DELIVERED",
        "occurred_at": "2026-10-09T15:30:00+08:00", "payload": {"signed_by": "张三"},
    }))

    step("8. 乱序事件：时间早于已接收最大时间，返回 422 EVENT_OUT_OF_ORDER")
    show(client.post(f"/v1/orders/{ORDER_ID}/events", json={
        "event_id": "EVT-DEMO-5", "type": "IN_TRANSIT",
        "occurred_at": "2026-09-29T08:00:00+08:00", "payload": {},
    }))

    step("9. 字段缺失：缺少 occurred_at，返回 422 VALIDATION_ERROR 并指明缺失字段")
    show(client.post(f"/v1/orders/{ORDER_ID}/events", json={
        "event_id": "EVT-DEMO-6", "type": "DELIVERED",
    }))

    step("10. 迟到的更正事件：签收时间更正为 2026-10-12 10:00，只产生新判定版本")
    show(client.post(f"/v1/orders/{ORDER_ID}/events", json={
        "event_id": "EVT-DEMO-7", "type": "CORRECTION",
        "occurred_at": "2026-10-12T12:00:00+08:00",
        "corrects_event_id": "EVT-DEMO-4",
        "corrected_occurred_at": "2026-10-12T10:00:00+08:00",
    }))

    step("11. 更正后的当前判定（截止时间随更正顺延）")
    show(client.get(f"/v1/orders/{ORDER_ID}/determination",
                    params={"as_of": "2026-10-13T10:00:00+08:00"}))

    step("12. 判定历史：旧版本已归档且内容不被改写")
    history = show(client.get(f"/v1/orders/{ORDER_ID}/determinations"))
    for h in history["history"]:
        det = h["determination"]
        print(f"  v{h['version']} archived={h['archived']} "
              f"breach={det['breach']} deadline={det['deadline']}")

    print("\n演示完成。")


if __name__ == "__main__":
    try:
        main()
    except httpx.ConnectError:
        print(f"无法连接 {BASE}，请先启动服务（见 README）。", file=sys.stderr)
        sys.exit(1)
