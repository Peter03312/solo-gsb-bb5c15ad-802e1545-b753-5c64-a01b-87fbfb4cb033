# 快递理赔时限判定服务

输入订单、承诺时限、时区、节假日表与按时间到达的扫描事件，输出**当前是否可申请理赔、
截止时间、缺失证据与判定依据**。规则按订单创建时生效的政策版本计算。

- 技术栈：Python 3.11+ / FastAPI / SQLite（WAL），时区使用 IANA 数据库（`zoneinfo`）
- 交互式 API 文档：服务启动后访问 `http://127.0.0.1:8000/docs`

## 快速开始

### 方式一：Docker Compose（推荐）

```bash
docker compose up --build
# init 容器先执行 scripts/init_db.py 完成建表与种子数据，随后 api 启动在 :8000
```

### 方式二：本地运行

```bash
pip install -r requirements-dev.txt     # 运行服务只需 requirements.txt
python scripts/init_db.py               # 初始化（幂等；服务启动时也会自动执行）
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 运行示例与测试

```bash
python examples/demo_client.py          # 端到端调用示例（需服务已启动）
bash examples/curl_examples.sh          # 等价的 curl 示例
python -m pytest tests/ -q              # 18 个规则测试
```

配置项：环境变量 `CLAIMS_DB_PATH` 指定 SQLite 路径（默认 `./claims.db`，镜像内为 `/data/claims.db`）。

## 判定规则

### 政策版本（按订单创建时生效）

创建订单时按**订单创建日（订单时区的当地日期）**解析生效的政策版本，并固化在订单上，
后续判定不受政策变更影响。种子数据：

| 版本 | 生效日期 | 理赔窗口 | 破损照片证据 |
|---|---|---|---|
| POLICY-2024 | 2024-01-01 | 触发点后 30 个**自然日** | 不要求 |
| POLICY-2025 | 2025-01-01 | 触发点后 15 个**工作日**（跳过周末与当地节假日） | 要求 |

### 触发点与截止时间

触发点（理赔窗口起点）按优先级取：

1. `LOST_DECLARED`（宣告遗失）→ 该事件时间
2. `DAMAGED_REPORTED`（破损上报）→ 该事件时间
3. `DELIVERED`（签收）→ 签收时间（签收晚于承诺截止则构成迟延送达）
4. 以上皆无 → 承诺违约点 `promised_by`

`deadline = 触发点 + 政策窗口`。工作日窗口在**订单时区的当地日历**上逐日推进，
跳过周六、周日与节假日表中的日期；跳过的日期明细会写入判定依据
（`weekends_skipped` / `holidays_skipped`）。

### 可申请条件（can_apply_now）

同时满足：存在可理赔违约情形（`LOST` / `DAMAGED` / `LATE_DELIVERY` / `NON_DELIVERY`）、
`window_open_at <= as_of <= deadline`、无缺失证据。否则 `reason_code` 指明原因：
`NO_BREACH` / `MISSING_EVIDENCE` / `WINDOW_NOT_OPEN` / `WINDOW_CLOSED`。

### 缺失证据（missing_evidence）

- `lost_declaration`：承诺期已过仍未签收时，需承运方宣告遗失才能提起未送达理赔
- `damage_photo`：POLICY-2025 起破损理赔须在 `DAMAGED_REPORTED` 事件的
  `payload.photo_evidence` 中附照片证据

### 事件流约束

- **幂等**：`event_id` 全局唯一。重放相同内容返回 `duplicate: true`，不重复计入、
  不产生新判定版本；同一 `event_id` 携带不同内容返回 `409 EVENT_ID_CONFLICT`。
- **保序**：事件 `occurred_at` 不得早于该订单已接收的最大事件时间，否则
  `422 EVENT_OUT_OF_ORDER`。历史事件的修正须走更正事件。
- **迟到更正**：`type=CORRECTION` 的事件携带 `corrects_event_id` 与
  `corrected_occurred_at`（可选 `corrected_type`）。更正只**追加新判定版本**，
  已归档判定（`GET .../determinations` 中 `archived=true`）永不改写；
  当前判定按更正后的有效时间线重新计算。
- **字段校验**：缺少必填字段或字段非法返回 `422 VALIDATION_ERROR`，
  `details.missing` / `details.invalid` 明确列出问题字段。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 健康检查 |
| GET | `/v1/policies` | 政策版本列表 |
| PUT | `/v1/holiday-calendars/{code}` | 整表替换节假日表 `{"holidays": [{"date", "name"}]}` |
| GET | `/v1/holiday-calendars/{code}` | 查询节假日表 |
| POST | `/v1/orders` | 创建订单（解析政策版本、计算承诺截止） |
| GET | `/v1/orders/{order_id}` | 订单详情 |
| POST | `/v1/orders/{order_id}/events` | 接入扫描事件（幂等、保序、支持更正） |
| GET | `/v1/orders/{order_id}/events` | 原始事件流水 + 更正后的有效时间线 |
| GET | `/v1/orders/{order_id}/determination?as_of=` | 当前判定（`as_of` 缺省为当前时间） |
| GET | `/v1/orders/{order_id}/determinations` | 判定历史（归档版本只读） |

### 判定响应示例

```json
{
  "order_id": "ORD-DEMO-0001",
  "as_of": "2026-10-12T02:00:00Z",
  "can_apply_now": true,
  "reason_code": "OK",
  "breach": "LATE_DELIVERY",
  "window_open_at": "2026-10-09T07:30:00Z",
  "deadline": "2026-10-30T07:30:00Z",
  "missing_evidence": [],
  "basis": {
    "policy_version": "POLICY-2025",
    "claim_window": {"type": "business_days", "value": 15},
    "timezone": "Asia/Shanghai",
    "holiday_calendar": "CN",
    "promised_by": "2026-10-08T02:00:00Z",
    "trigger": {"kind": "DELIVERED", "event_id": "EVT-DEMO-4",
                "occurred_at": "2026-10-09T07:30:00Z"},
    "weekends_skipped": ["2026-10-10", "2026-10-11", "..."],
    "holidays_skipped": [],
    "corrections_applied": []
  },
  "determination_version": 4
}
```

### 错误响应格式

```json
{"error": {"code": "EVENT_OUT_OF_ORDER", "message": "...", "details": {...}}}
```

错误码：`VALIDATION_ERROR` / `ORDER_NOT_FOUND` / `ORDER_EXISTS` / `UNKNOWN_CALENDAR` /
`CALENDAR_NOT_FOUND` / `NO_EFFECTIVE_POLICY` / `EVENT_ID_CONFLICT` / `EVENT_OUT_OF_ORDER` /
`CORRECTION_TARGET_NOT_FOUND` / `CANNOT_CORRECT_CORRECTION` / `TARGET_ALREADY_CORRECTED` /
`INVALID_AS_OF`。

## 目录结构

```
app/
  main.py       # FastAPI 应用与路由、统一错误处理
  engine.py     # 判定引擎：有效时间线、判定计算、事件接入（幂等/保序/更正）
  storage.py    # SQLite 持久层（事件与判定只追加，归档不可改写）
  schemas.py    # 请求模型与字段校验
  policies      # 见 app/seed.py 种子数据与 app/storage.py resolve_policy
  bizdays.py    # 工作日推算（跳过周末与当地节假日）
  seed.py       # 初始化种子：政策版本、CN/US 示例节假日
scripts/init_db.py        # 初始化脚本（幂等）
examples/demo_client.py   # Python 端到端调用示例
examples/curl_examples.sh # curl 调用示例
tests/test_service.py     # 规则测试（18 个用例）
Dockerfile / docker-compose.yml
```

> 说明：种子节假日为演示数据；生产环境请用 `PUT /v1/holiday-calendars/{code}`
> 维护权威节假日表。政策版本目前由种子数据提供，如需新增版本可向 `policies`
> 表插入记录（`scripts/init_db.py` 是扩展入口）。
