#!/usr/bin/env bash
# 端到端 curl 调用示例。前置：服务已运行（默认 http://127.0.0.1:8000）。
set -euo pipefail

BASE="${BASE_URL:-http://127.0.0.1:8000}"
ORDER_ID="${DEMO_ORDER_ID:-ORD-CURL-0001}"
fmt() { python3 -m json.tool --no-ensure-ascii 2>/dev/null || cat; }

echo "== 0. 健康检查 =="
curl -sS "$BASE/healthz" | fmt

echo "== 1. 政策版本列表 =="
curl -sS "$BASE/v1/policies" | fmt

echo "== 2. 创建订单（2026-09-28 创建，承诺 3 个工作日，CN 节假日表）=="
curl -sS -X POST "$BASE/v1/orders" -H 'Content-Type: application/json' -d "{
  \"order_id\": \"$ORDER_ID\",
  \"created_at\": \"2026-09-28T10:00:00+08:00\",
  \"timezone\": \"Asia/Shanghai\",
  \"promise_type\": \"business_days\",
  \"promise_value\": 3,
  \"holiday_calendar\": \"CN\"
}" | fmt

echo "== 3. 推送扫描事件 =="
curl -sS -X POST "$BASE/v1/orders/$ORDER_ID/events" -H 'Content-Type: application/json' -d '{
  "event_id": "EVT-CURL-1", "type": "PICKED_UP",
  "occurred_at": "2026-09-28T14:00:00+08:00", "payload": {"site": "上海浦东"}
}' | fmt
curl -sS -X POST "$BASE/v1/orders/$ORDER_ID/events" -H 'Content-Type: application/json' -d '{
  "event_id": "EVT-CURL-2", "type": "DELIVERED",
  "occurred_at": "2026-10-09T15:30:00+08:00", "payload": {"signed_by": "张三"}
}' | fmt

echo "== 4. 查询当前判定（as_of 可省略，默认当前时间）=="
curl -sS "$BASE/v1/orders/$ORDER_ID/determination?as_of=2026-10-12T10:00:00%2B08:00" | fmt

echo "== 5. 幂等重放同一事件（duplicate=true，不产生新版本）=="
curl -sS -X POST "$BASE/v1/orders/$ORDER_ID/events" -H 'Content-Type: application/json' -d '{
  "event_id": "EVT-CURL-2", "type": "DELIVERED",
  "occurred_at": "2026-10-09T15:30:00+08:00", "payload": {"signed_by": "张三"}
}' | fmt

echo "== 6. 乱序事件（返回 422 EVENT_OUT_OF_ORDER）=="
curl -sS -X POST "$BASE/v1/orders/$ORDER_ID/events" -H 'Content-Type: application/json' -d '{
  "event_id": "EVT-CURL-3", "type": "IN_TRANSIT",
  "occurred_at": "2026-09-29T08:00:00+08:00", "payload": {}
}' | fmt

echo "== 7. 字段缺失（返回 422 VALIDATION_ERROR，指明缺失字段）=="
curl -sS -X POST "$BASE/v1/orders/$ORDER_ID/events" -H 'Content-Type: application/json' -d '{
  "event_id": "EVT-CURL-4", "type": "DELIVERED"
}' | fmt

echo "== 8. 迟到的更正事件（只产生新判定版本）=="
curl -sS -X POST "$BASE/v1/orders/$ORDER_ID/events" -H 'Content-Type: application/json' -d '{
  "event_id": "EVT-CURL-5", "type": "CORRECTION",
  "occurred_at": "2026-10-12T12:00:00+08:00",
  "corrects_event_id": "EVT-CURL-2",
  "corrected_occurred_at": "2026-10-12T10:00:00+08:00"
}' | fmt

echo "== 9. 判定历史（旧版本 archived=true，内容不被改写）=="
curl -sS "$BASE/v1/orders/$ORDER_ID/determinations" | fmt
