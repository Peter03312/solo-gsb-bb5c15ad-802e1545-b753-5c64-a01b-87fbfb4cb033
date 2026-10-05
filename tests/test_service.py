"""规则测试：政策版本、工作日跳节假日、幂等、乱序、缺字段、迟到更正、缺失证据。"""

AS_OF_AFTER = "2026-10-12T10:00:00+08:00"


def _post_event(client, order_id, **kw):
    return client.post(f"/v1/orders/{order_id}/events", json=kw)


def _determination(client, order_id, as_of=AS_OF_AFTER):
    resp = client.get(f"/v1/orders/{order_id}/determination", params={"as_of": as_of})
    assert resp.status_code == 200, resp.text
    return resp.json()


# ------------------------------------------------------------ 订单与政策版本

def test_order_resolves_policy_and_promise_skips_holidays(client):
    resp = client.post("/v1/orders", json={
        "order_id": "ORD-T-1",
        "created_at": "2026-09-28T10:00:00+08:00",
        "timezone": "Asia/Shanghai",
        "promise_type": "business_days",
        "promise_value": 3,
        "holiday_calendar": "CN",
    })
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert created["policy_version"] == "POLICY-2025"          # 2026 年创建 -> 2025 版政策
    assert created["promised_by"] == "2026-10-08T02:00:00Z"    # 10/8 10:00(+08)，跳过 10/1-10/7
    assert created["promise_basis"]["holidays_skipped"] == [
        f"2026-10-0{d}" for d in range(1, 8)]

    order = client.get("/v1/orders/ORD-T-1").json()
    assert order["policy_version"] == "POLICY-2025"
    assert order["promised_by"] == "2026-10-08T02:00:00Z"


def test_policy_2024_uses_calendar_days(client):
    resp = client.post("/v1/orders", json={
        "order_id": "ORD-2024", "created_at": "2024-06-01T10:00:00+08:00",
        "timezone": "Asia/Shanghai", "promise_type": "hours", "promise_value": 72,
        "holiday_calendar": "CN",
    })
    assert resp.status_code == 201, resp.text
    assert resp.json()["policy_version"] == "POLICY-2024"
    assert resp.json()["promised_by"] == "2024-06-04T02:00:00Z"  # +72h

    _post_event(client, "ORD-2024", event_id="E1", type="DELIVERED",
                occurred_at="2024-06-05T10:00:00+08:00", payload={})
    det = _determination(client, "ORD-2024", as_of="2024-06-10T00:00:00+08:00")
    assert det["breach"] == "LATE_DELIVERY"
    assert det["can_apply_now"] is True
    # 2024 版：30 个自然日，不跳节假日
    assert det["deadline"] == "2024-07-05T02:00:00Z"
    assert det["basis"]["claim_window"] == {"type": "calendar_days", "value": 30}


def test_unknown_calendar_and_bad_timezone_and_naive_time(client):
    resp = client.post("/v1/orders", json={
        "order_id": "ORD-X", "created_at": "2026-09-28T10:00:00+08:00",
        "timezone": "Asia/Shanghai", "promise_type": "hours", "promise_value": 24,
        "holiday_calendar": "NOPE",
    })
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "UNKNOWN_CALENDAR"

    resp = client.post("/v1/orders", json={
        "order_id": "ORD-X", "created_at": "2026-09-28T10:00:00+08:00",
        "timezone": "Mars/Olympus", "promise_type": "hours", "promise_value": 24,
        "holiday_calendar": "CN",
    })
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    resp = client.post("/v1/orders", json={
        "order_id": "ORD-X", "created_at": "2026-09-28T10:00:00",  # 无时区偏移
        "timezone": "Asia/Shanghai", "promise_type": "hours", "promise_value": 24,
        "holiday_calendar": "CN",
    })
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


# ------------------------------------------------------------ 工作日截止

def test_business_day_deadline_skips_weekends(client, order_id):
    _post_event(client, order_id, event_id="E1", type="DELIVERED",
                occurred_at="2026-10-09T15:30:00+08:00", payload={})
    det = _determination(client, order_id)
    assert det["breach"] == "LATE_DELIVERY"
    assert det["can_apply_now"] is True
    assert det["missing_evidence"] == []
    # 触发点 10/9(周五) 15:30 + 15 个工作日 -> 10/30 15:30(+08)
    assert det["deadline"] == "2026-10-30T07:30:00Z"
    assert det["basis"]["weekends_skipped"] == [
        "2026-10-10", "2026-10-11", "2026-10-17", "2026-10-18",
        "2026-10-24", "2026-10-25"]
    assert det["basis"]["holidays_skipped"] == []


def test_business_day_deadline_skips_holidays(client, order_id):
    _post_event(client, order_id, event_id="E1", type="LOST_DECLARED",
                occurred_at="2026-09-30T08:00:00+08:00", payload={})
    det = _determination(client, order_id)
    assert det["breach"] == "LOST"
    assert det["can_apply_now"] is True
    # 触发点 9/30 + 15 个工作日，跨过国庆 10/1-10/7 -> 10/28 08:00(+08)
    assert det["deadline"] == "2026-10-28T00:00:00Z"
    assert det["basis"]["holidays_skipped"] == [f"2026-10-0{d}" for d in range(1, 8)]


def test_window_closed_after_deadline(client, order_id):
    _post_event(client, order_id, event_id="E1", type="DELIVERED",
                occurred_at="2026-10-09T15:30:00+08:00", payload={})
    det = _determination(client, order_id, as_of="2026-11-05T10:00:00+08:00")
    assert det["can_apply_now"] is False
    assert det["reason_code"] == "WINDOW_CLOSED"


# ------------------------------------------------------------ 幂等与乱序

def test_replay_same_event_not_counted_twice(client, order_id):
    body = {"event_id": "E1", "type": "PICKED_UP",
            "occurred_at": "2026-09-28T14:00:00+08:00", "payload": {"site": "浦东"}}
    r1 = _post_event(client, order_id, **body)
    assert r1.status_code == 201 and r1.json()["applied"] is True
    r2 = _post_event(client, order_id, **body)
    assert r2.status_code == 201 and r2.json()["duplicate"] is True
    assert r2.json()["determination_version"] == r1.json()["determination_version"] == 1
    history = client.get(f"/v1/orders/{order_id}/determinations").json()["history"]
    assert len(history) == 1  # 重放未产生新判定


def test_same_event_id_different_content_conflicts(client, order_id):
    _post_event(client, order_id, event_id="E1", type="PICKED_UP",
                occurred_at="2026-09-28T14:00:00+08:00", payload={})
    resp = _post_event(client, order_id, event_id="E1", type="IN_TRANSIT",
                       occurred_at="2026-09-29T08:00:00+08:00", payload={})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "EVENT_ID_CONFLICT"


def test_out_of_order_event_rejected(client, order_id):
    _post_event(client, order_id, event_id="E1", type="DELIVERED",
                occurred_at="2026-10-09T15:30:00+08:00", payload={})
    resp = _post_event(client, order_id, event_id="E2", type="IN_TRANSIT",
                       occurred_at="2026-09-29T08:00:00+08:00", payload={})
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "EVENT_OUT_OF_ORDER"
    assert err["details"]["max_occurred_at"] == "2026-10-09T07:30:00Z"


def test_missing_field_returns_clear_error(client, order_id):
    resp = _post_event(client, order_id, event_id="E1", type="DELIVERED")
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "VALIDATION_ERROR"
    assert "occurred_at" in err["details"]["missing"]


# ------------------------------------------------------------ 缺失证据

def test_non_delivery_requires_lost_declaration(client, order_id):
    det = _determination(client, order_id, as_of="2026-10-10T10:00:00+08:00")
    assert det["breach"] == "NON_DELIVERY"
    assert det["can_apply_now"] is False
    assert det["reason_code"] == "MISSING_EVIDENCE"
    assert det["missing_evidence"] == ["lost_declaration"]
    assert det["basis"]["trigger"]["kind"] == "PROMISE_BREACH"

    _post_event(client, order_id, event_id="E1", type="LOST_DECLARED",
                occurred_at="2026-10-10T12:00:00+08:00", payload={})
    det = _determination(client, order_id)
    assert det["breach"] == "LOST"
    assert det["can_apply_now"] is True
    assert det["missing_evidence"] == []


def test_damage_photo_required_by_policy_2025(client, order_id):
    _post_event(client, order_id, event_id="E1", type="DELIVERED",
                occurred_at="2026-09-30T10:00:00+08:00", payload={})  # 按时送达
    _post_event(client, order_id, event_id="E2", type="DAMAGED_REPORTED",
                occurred_at="2026-10-02T09:00:00+08:00", payload={})  # 未附照片
    det = _determination(client, order_id, as_of="2026-10-05T10:00:00+08:00")
    assert det["breach"] == "DAMAGED"
    assert det["can_apply_now"] is False
    assert det["missing_evidence"] == ["damage_photo"]


def test_damage_with_photo_can_apply(client, order_id):
    _post_event(client, order_id, event_id="E1", type="DELIVERED",
                occurred_at="2026-09-30T10:00:00+08:00", payload={})
    _post_event(client, order_id, event_id="E2", type="DAMAGED_REPORTED",
                occurred_at="2026-10-02T09:00:00+08:00",
                payload={"photo_evidence": True})
    det = _determination(client, order_id, as_of="2026-10-05T10:00:00+08:00")
    assert det["can_apply_now"] is True
    assert det["missing_evidence"] == []


def test_delivered_on_time_no_breach(client, order_id):
    _post_event(client, order_id, event_id="E1", type="DELIVERED",
                occurred_at="2026-10-08T09:00:00+08:00", payload={})  # 承诺 10-08 10:00 前
    det = _determination(client, order_id)
    assert det["breach"] == "NONE"
    assert det["can_apply_now"] is False
    assert det["reason_code"] == "NO_BREACH"


# ------------------------------------------------------------ 迟到更正

def test_correction_creates_new_version_and_keeps_archive(client, order_id):
    _post_event(client, order_id, event_id="E1", type="PICKED_UP",
                occurred_at="2026-09-28T14:00:00+08:00", payload={})
    _post_event(client, order_id, event_id="E2", type="DELIVERED",
                occurred_at="2026-10-09T15:30:00+08:00", payload={})
    before = client.get(f"/v1/orders/{order_id}/determinations").json()["history"]
    assert len(before) == 2
    archived_v2 = before[-1]
    assert archived_v2["determination"]["deadline"] == "2026-10-30T07:30:00Z"

    # 迟到的更正：签收时间实为 2026-10-12 10:00
    resp = _post_event(
        client, order_id, event_id="E3", type="CORRECTION",
        occurred_at="2026-10-12T12:00:00+08:00",
        corrects_event_id="E2", corrected_occurred_at="2026-10-12T10:00:00+08:00")
    assert resp.status_code == 201, resp.text
    assert resp.json()["determination_version"] == 3

    det = _determination(client, order_id, as_of="2026-10-13T10:00:00+08:00")
    assert det["determination_version"] == 3
    assert det["deadline"] == "2026-11-02T02:00:00Z"  # 10/12 + 15 个工作日
    assert det["basis"]["corrections_applied"] == [{
        "corrects_event_id": "E2",
        "correction_event_id": "E3",
        "original_occurred_at": "2026-10-09T07:30:00Z",
        "corrected_occurred_at": "2026-10-12T02:00:00Z",
    }]

    after = client.get(f"/v1/orders/{order_id}/determinations").json()
    assert after["current_version"] == 3
    v2 = after["history"][1]
    assert v2["archived"] is True
    # 已归档判定不被改写：内容与更正前完全一致
    assert v2["determination"] == archived_v2["determination"]
    assert v2["determination"]["deadline"] == "2026-10-30T07:30:00Z"
    assert after["history"][2]["archived"] is False


def test_correction_target_must_exist(client, order_id):
    resp = _post_event(
        client, order_id, event_id="E9", type="CORRECTION",
        occurred_at="2026-10-12T12:00:00+08:00",
        corrects_event_id="NOPE", corrected_occurred_at="2026-10-12T10:00:00+08:00")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "CORRECTION_TARGET_NOT_FOUND"


def test_correction_requires_correction_fields(client, order_id):
    resp = _post_event(client, order_id, event_id="E9", type="CORRECTION",
                       occurred_at="2026-10-12T12:00:00+08:00")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_correction_replay_is_idempotent(client, order_id):
    _post_event(client, order_id, event_id="E1", type="DELIVERED",
                occurred_at="2026-10-09T15:30:00+08:00", payload={})
    body = {"event_id": "E2", "type": "CORRECTION",
            "occurred_at": "2026-10-12T12:00:00+08:00",
            "corrects_event_id": "E1",
            "corrected_occurred_at": "2026-10-12T10:00:00+08:00"}
    r1 = _post_event(client, order_id, **body)
    r2 = _post_event(client, order_id, **body)
    assert r1.json()["applied"] is True
    assert r2.json()["duplicate"] is True
    history = client.get(f"/v1/orders/{order_id}/determinations").json()["history"]
    assert len(history) == 2
