from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from app.core.clock import FrozenClock
from app.core.errors import ConflictError, NotFoundError
from app.database import get_connection, transaction
from app.germplasm.service import GermplasmService
from app.germplasm.viability import add_months

TODAY = datetime(2026, 9, 30, 8, 0, tzinfo=UTC)


def make_service(clock: FrozenClock) -> GermplasmService:
    return GermplasmService(get_connection(), clock)


def create_lot_with_completed_test(
    service: GermplasmService,
    clock: FrozenClock,
    suffix: str,
    germination: int,
    completed_at: datetime,
    crop: str = "水稻",
) -> dict:
    source = service.accessions.create_source({
        "source_code": f"SRC-{suffix}", "provider_name": "省级采集队", "country_code": "CN",
        "locality": "河谷试验站", "collected_on": "2025-10-02", "permit_reference": None,
        "restrictions": {},
    })
    accession = service.accessions.create_accession({
        "accession_no": f"ACC-{suffix}", "scientific_name": "Oryza sativa", "crop_name": crop,
        "cultivar_name": "地方材料", "source_id": source["id"], "acquisition_type": "采集",
        "received_on": "2025-12-01", "passport": {}, "created_by": "登记员",
    })
    service.accessions.transition(accession["id"], {
        "target_status": "accepted", "reason": "资料齐全", "expected_version": 1, "actor": "审核员",
    })
    location = service.inventory.create_location({
        "location_code": f"COLD-{suffix}", "facility": "长期库", "room": "低温一室", "rack": "R1", "shelf": "S1",
        "capacity_grams": 1000, "temperature_c": -18, "humidity_percent": 30,
    })
    lot = service.inventory.create_lot({
        "lot_no": f"LOT-{suffix}", "accession_id": accession["id"], "parent_lot_id": None,
        "harvest_year": 2025, "initial_weight_grams": 500, "moisture_percent": 7.5,
        "treatment": "清选干燥", "sealed_on": "2025-12-20", "created_by": "登记员",
    })
    service.inventory.place_lot({
        "lot_id": lot["id"], "location_id": location["id"], "weight_grams": 500,
        "container_code": f"BOX-{suffix}", "idempotency_key": f"place-{suffix}", "actor": "保管员",
    })
    protocol = service.viability.create_protocol({
        "protocol_code": f"GER-{suffix}", "crop_name": crop, "sample_size": 100, "replicate_count": 1,
        "temperature_c": 25, "duration_days": 14, "normal_seedling_rule": "根芽发育完整",
        "created_by": "技术负责人",
    })
    clock.current = completed_at - timedelta(days=1)
    test = service.viability.schedule_test({
        "test_no": f"VT-{suffix}", "lot_id": lot["id"], "protocol_id": protocol["id"],
        "test_type": "周期复检", "sampled_grams": 5, "scheduled_for": completed_at.date().isoformat(),
        "requested_by": "检测员", "idempotency_key": f"schedule-{suffix}",
    })
    service.viability.start_test(test["id"], {"performed_by": "检测员", "expected_version": 1})
    clock.current = completed_at
    service.viability.add_count(test["id"], {
        "replicate_no": 1, "seeds_tested": 100, "normal_count": germination,
        "abnormal_count": 0, "dead_count": 100 - germination, "fresh_count": 0,
        "observation_day": 14, "observed_by": "检测员",
    })
    completed = service.viability.complete_test(test["id"], {"performed_by": "检测员", "expected_version": 2})
    return {"lot": service.repository.require_lot(lot["id"]), "test": completed}


@pytest.fixture()
def governed(client):
    """四个高风险批次（A/B/D/E）+ 一个低风险批次（C），旧策略 12 个月。"""
    clock = FrozenClock(datetime(2026, 1, 1, 8, 0, tzinfo=UTC))
    service = make_service(clock)
    with transaction(immediate=True):
        old_policy = service.viability.create_policy({
            "crop_name": "水稻", "risk_level": "high", "interval_months": 12, "warning_days": 30,
            "minimum_germination_percent": 70, "effective_from": "2026-01-01", "effective_to": None,
            "created_by": "质量负责人",
        })
        lot_a = create_lot_with_completed_test(service, clock, "A01", 60, datetime(2026, 1, 15, 9, 0, tzinfo=UTC))
        lot_b = create_lot_with_completed_test(service, clock, "B01", 65, datetime(2026, 3, 10, 9, 0, tzinfo=UTC))
        lot_c = create_lot_with_completed_test(service, clock, "C01", 90, datetime(2026, 2, 5, 9, 0, tzinfo=UTC))
        lot_d = create_lot_with_completed_test(service, clock, "D01", 60, datetime(2026, 2, 1, 9, 0, tzinfo=UTC))
        lot_e = create_lot_with_completed_test(service, clock, "E01", 62, datetime(2026, 2, 10, 9, 0, tzinfo=UTC))
        # D 的日程被人工豁免
        waived = service.repository.active_retest_schedule(lot_d["lot"]["id"])
        service.viability.waive_schedule(waived["id"], {"actor": "质量负责人", "reason": "等待出库，暂缓复检"})
        # E 已安排新检测，日程进入 scheduled
        protocol_e = service.connection.execute(
            "SELECT id FROM viability_protocols WHERE protocol_code='GER-E01'"
        ).fetchone()[0]
        clock.current = datetime(2026, 9, 20, 8, 0, tzinfo=UTC)
        service.viability.schedule_test({
            "test_no": "VT-E01-2", "lot_id": lot_e["lot"]["id"], "protocol_id": protocol_e,
            "test_type": "周期复检", "sampled_grams": 5, "scheduled_for": "2026-10-05",
            "requested_by": "检测员", "idempotency_key": "schedule-e01-2",
        })
        # 候选策略：高风险复检间隔 12 → 6 个月
        clock.current = TODAY
        revision = service.policy_revisions.create_revision({
            "crop_name": "水稻", "risk_level": "high", "interval_months": 6, "warning_days": 30,
            "minimum_germination_percent": 70, "effective_from": "2026-09-30", "effective_to": None,
            "change_note": "高风险作物复检间隔缩短", "created_by": "质量负责人",
        })
    return {
        "service": service, "clock": clock, "revision": revision, "old_policy": old_policy,
        "lots": {"A": lot_a, "B": lot_b, "C": lot_c, "D": lot_d, "E": lot_e},
    }


def test_preview_computes_impact_conflicts_and_overdue(governed):
    service = governed["service"]
    revision = governed["revision"]
    lots = governed["lots"]
    with transaction(immediate=True):
        summary = service.policy_revisions.preview(revision["id"], {"actor": "质量负责人"})
    assert summary["affected_lots"] == 4  # C 为低风险，不在范围
    assert summary["recompute_count"] == 2
    assert summary["conflict_count"] == 2
    assert summary["becomes_overdue_count"] == 2
    assert summary["no_longer_overdue_count"] == 0
    assert summary["notify_count"] == 2
    with transaction(immediate=True):
        detail = service.policy_revisions.get_preview(revision["id"])
        conflicts = service.policy_revisions.conflicts(revision["id"])
    items = {item["lot_no"]: item for item in detail["items"]}
    assert items["LOT-A01"]["old_due_on"] == "2027-01-15"
    assert items["LOT-A01"]["new_due_on"] == "2026-07-15"
    assert items["LOT-A01"]["action"] == "recompute"
    assert items["LOT-A01"]["new_overdue"] == 1
    assert items["LOT-B01"]["new_due_on"] == "2026-09-10"
    assert items["LOT-D01"]["action"] == "keep_waived"
    assert items["LOT-E01"]["action"] == "keep_scheduled"
    assert {item["lot_no"] for item in conflicts["items"]} == {"LOT-D01", "LOT-E01"}
    assert lots["C"]["lot"]["lot_no"] == "LOT-C01"  # 低风险批次未受影响


def test_approve_requires_preview(governed):
    service = governed["service"]
    revision = governed["revision"]
    with transaction(immediate=True):
        with pytest.raises(ConflictError):
            service.policy_revisions.approve(revision["id"], {"actor": "质量负责人"})
        service.policy_revisions.preview(revision["id"], {"actor": "质量负责人"})
        approved = service.policy_revisions.approve(revision["id"], {"actor": "质量负责人"})
    assert approved["status"] == "approved"
    assert approved["approved_by"] == "质量负责人"


def test_publish_recomputes_only_eligible_and_keeps_history(governed):
    service = governed["service"]
    revision = governed["revision"]
    lots = governed["lots"]
    with transaction(immediate=True):
        service.policy_revisions.preview(revision["id"], {"actor": "质量负责人"})
        service.policy_revisions.approve(revision["id"], {"actor": "质量负责人"})
    summary = service.policy_revisions.publish(revision["id"], {"actor": "质量负责人"})
    assert summary["publication"]["status"] == "completed"
    assert summary["publication"]["processed_lots"] == 4
    assert summary["action_counts"] == {"kept_scheduled": 1, "kept_waived": 1, "recomputed": 2}
    assert summary["revision"]["status"] == "published"
    assert summary["publication"]["notifications_enqueued"] == 2

    connection = get_connection()
    # A/B 被重算：旧日程 superseded，新日程指向新版本
    active_a = service.repository.active_retest_schedule(lots["A"]["lot"]["id"])
    assert active_a["due_on"] == "2026-07-15"
    assert active_a["policy_id"] == revision["id"]
    assert active_a["status"] == "notified"  # 已逾期，提醒已入队
    history = connection.execute(
        "SELECT status,due_on,policy_id FROM retest_schedules WHERE lot_id=? ORDER BY id",
        (lots["A"]["lot"]["id"],),
    ).fetchall()
    assert [(row[0], row[1]) for row in history] == [("superseded", "2027-01-15"), ("notified", "2026-07-15")]
    assert history[0][2] == governed["old_policy"]["id"]  # 历史依据保留旧策略版本
    # D 人工豁免保留，E 已安排检测保留
    waived = connection.execute(
        "SELECT status,waived_by FROM retest_schedules WHERE lot_id=?", (lots["D"]["lot"]["id"],)
    ).fetchone()
    assert waived[0] == "waived" and waived[1] == "质量负责人"
    arranged = connection.execute(
        "SELECT status FROM retest_schedules WHERE lot_id=?", (lots["E"]["lot"]["id"],)
    ).fetchone()
    assert arranged[0] == "scheduled"
    # 已完成检测的历史结果不变
    test_a = service.repository.require_test(lots["A"]["test"]["id"])
    assert test_a["status"] == "completed"
    assert test_a["germination_percent"] == 60
    assert test_a["completed_at"] == lots["A"]["test"]["completed_at"]
    # 提醒事件入队且键确定
    events = connection.execute(
        "SELECT event_key,event_type FROM outbox_events WHERE event_type='retest.schedule.reminder' ORDER BY event_key"
    ).fetchall()
    assert len(events) == 2
    # 旧策略版本被取代但记录保留
    old = service.repository.require_policy(governed["old_policy"]["id"])
    assert old["status"] == "superseded"
    assert old["interval_months"] == 12
    # 发布后的确定性到期列表
    due = service.policy_revisions.due_list(revision["id"])
    assert due["total"] == 4
    effective = {item["lot_no"]: item["effective_due_on"] for item in due["items"]}
    assert effective == {
        "LOT-A01": "2026-07-15", "LOT-B01": "2026-09-10",
        "LOT-D01": "2027-02-01", "LOT-E01": "2027-02-10",
    }


def test_publish_is_idempotent(governed):
    service = governed["service"]
    revision = governed["revision"]
    lots = governed["lots"]
    with transaction(immediate=True):
        service.policy_revisions.preview(revision["id"], {"actor": "质量负责人"})
        service.policy_revisions.approve(revision["id"], {"actor": "质量负责人"})
    first = service.policy_revisions.publish(revision["id"], {"actor": "质量负责人"})
    second = service.policy_revisions.publish(revision["id"], {"actor": "质量负责人"})
    assert second["publication"]["status"] == "completed"
    assert second["publication"]["attempts"] == first["publication"]["attempts"]
    connection = get_connection()
    active_count = connection.execute(
        "SELECT COUNT(*) FROM retest_schedules WHERE lot_id=? AND status IN ('pending','notified')",
        (lots["A"]["lot"]["id"],),
    ).fetchone()[0]
    assert active_count == 1  # 重复发布不会制造第二条有效日程
    reminders = connection.execute(
        "SELECT COUNT(*) FROM outbox_events WHERE event_type='retest.schedule.reminder'"
    ).fetchone()[0]
    assert reminders == 2  # 不会重复提醒
    assert connection.execute("SELECT COUNT(*) FROM retest_policy_publication_items").fetchone()[0] == 4


def test_publish_resumes_in_batches(governed):
    service = governed["service"]
    revision = governed["revision"]
    with transaction(immediate=True):
        service.policy_revisions.preview(revision["id"], {"actor": "质量负责人"})
        service.policy_revisions.approve(revision["id"], {"actor": "质量负责人"})
    first = service.policy_revisions.publish(revision["id"], {"actor": "质量负责人", "batch_size": 1})
    assert first["publication"]["status"] == "running"
    assert first["publication"]["processed_lots"] == 1
    cursor = first["publication"]["cursor_lot_id"]
    assert cursor > 0
    second = service.policy_revisions.publish(revision["id"], {"actor": "质量负责人", "batch_size": 2})
    assert second["publication"]["processed_lots"] == 3
    third = service.policy_revisions.publish(revision["id"], {"actor": "质量负责人"})
    assert third["publication"]["status"] == "completed"
    assert third["publication"]["processed_lots"] == 4


def test_publish_recovers_from_interruption(governed):
    service = governed["service"]
    revision = governed["revision"]
    lots = governed["lots"]
    with transaction(immediate=True):
        service.policy_revisions.preview(revision["id"], {"actor": "质量负责人"})
        service.policy_revisions.approve(revision["id"], {"actor": "质量负责人"})
    original = service.policy_revisions._apply_to_lot
    failing_lot = lots["B"]["lot"]["id"]

    def flaky(rev, publication, row, today):
        if int(row["lot_id"]) == failing_lot:
            raise RuntimeError("模拟发布中断")
        return original(rev, publication, row, today)

    service.policy_revisions._apply_to_lot = flaky
    with pytest.raises(RuntimeError):
        service.policy_revisions.publish(revision["id"], {"actor": "质量负责人"})
    failed = service.policy_revisions.revision_detail(revision["id"])
    assert failed["publication"]["status"] == "failed"
    assert failed["publication"]["processed_lots"] == 1  # A 已提交，B 回滚
    # 中断后从断点恢复，不重复处理已完成的批次
    service.policy_revisions._apply_to_lot = original
    summary = service.policy_revisions.publish(revision["id"], {"actor": "质量负责人"})
    assert summary["publication"]["status"] == "completed"
    assert summary["publication"]["processed_lots"] == 4
    assert summary["publication"]["attempts"] == 2
    connection = get_connection()
    assert connection.execute(
        "SELECT COUNT(*) FROM retest_schedules WHERE lot_id=? AND status IN ('pending','notified')",
        (lots["A"]["lot"]["id"],),
    ).fetchone()[0] == 1
    assert connection.execute(
        "SELECT COUNT(*) FROM outbox_events WHERE event_type='retest.schedule.reminder'"
    ).fetchone()[0] == 2


def test_rollback_creates_new_version_and_restores_dates(governed):
    service = governed["service"]
    revision = governed["revision"]
    lots = governed["lots"]
    with transaction(immediate=True):
        service.policy_revisions.preview(revision["id"], {"actor": "质量负责人"})
        service.policy_revisions.approve(revision["id"], {"actor": "质量负责人"})
    service.policy_revisions.publish(revision["id"], {"actor": "质量负责人"})
    with transaction(immediate=True):
        rollback = service.policy_revisions.rollback({
            "crop_name": "水稻", "risk_level": "high", "target_version": 1,
            "effective_from": "2026-10-01", "created_by": "质量负责人",
        })
    # 回滚生成新版本，历史版本不被改写
    assert rollback["version"] == 3
    assert rollback["status"] == "draft"
    assert rollback["interval_months"] == 12
    assert rollback["source_policy_id"] == governed["old_policy"]["id"]
    assert service.repository.require_policy(governed["old_policy"]["id"])["status"] == "superseded"
    assert service.repository.require_policy(revision["id"])["status"] == "published"
    with transaction(immediate=True):
        service.policy_revisions.preview(rollback["id"], {"actor": "质量负责人"})
        service.policy_revisions.approve(rollback["id"], {"actor": "质量负责人"})
    summary = service.policy_revisions.publish(rollback["id"], {"actor": "质量负责人"})
    assert summary["publication"]["status"] == "completed"
    # A 的到期日恢复为 12 个月间隔对应的日期（与历史日程重合，复用原行而非报错）
    active_a = service.repository.active_retest_schedule(lots["A"]["lot"]["id"])
    assert active_a["due_on"] == "2027-01-15"
    assert active_a["policy_id"] == rollback["id"]
    connection = get_connection()
    assert connection.execute(
        "SELECT COUNT(*) FROM retest_schedules WHERE lot_id=? AND status IN ('pending','notified')",
        (lots["A"]["lot"]["id"],),
    ).fetchone()[0] == 1
    # 回滚版本发布后，6 个月版本被取代但记录仍在
    assert service.repository.require_policy(revision["id"])["status"] == "superseded"


def test_explain_lot_as_of_shows_policy_version_in_effect(governed):
    service = governed["service"]
    revision = governed["revision"]
    lots = governed["lots"]
    with transaction(immediate=True):
        service.policy_revisions.preview(revision["id"], {"actor": "质量负责人"})
        service.policy_revisions.approve(revision["id"], {"actor": "质量负责人"})
    service.policy_revisions.publish(revision["id"], {"actor": "质量负责人"})
    lot_id = lots["A"]["lot"]["id"]
    before = service.policy_revisions.explain_lot(lot_id, "2026-06-01")
    assert before["crop_name"] == "水稻"
    assert before["risk_level"] == "high"
    assert before["latest_valid_test"]["test_no"] == "VT-A01"
    assert before["policy"]["version"] == 1
    assert before["policy"]["interval_months"] == 12
    assert before["expected_due_on"] == "2027-01-15"
    after = service.policy_revisions.explain_lot(lot_id, "2026-09-30")
    assert after["policy"]["version"] == 2
    assert after["policy"]["interval_months"] == 6
    assert after["expected_due_on"] == "2026-07-15"
    assert after["current_schedule"]["policy_version"] == 2
    assert any("策略" in line and "第 2 版" in line for line in after["explanation"])
    # 没有任何检测的时点
    early = service.policy_revisions.explain_lot(lot_id, "2025-12-31")
    assert early["latest_valid_test"] is None
    assert early["risk_level"] is None
    assert early["policy"] is None
    assert any("没有已完成的有效检测" in line for line in early["explanation"])


def test_due_list_requires_completed_publish(governed):
    service = governed["service"]
    revision = governed["revision"]
    with pytest.raises(ConflictError):
        service.policy_revisions.due_list(revision["id"])
    with pytest.raises(NotFoundError):
        service.policy_revisions.get_preview(revision["id"])


def test_policy_revision_api_flow(client, admin):
    headers = admin["headers"]
    clock = FrozenClock(datetime(2026, 3, 1, 8, 0, tzinfo=UTC))
    service = make_service(clock)
    with transaction(immediate=True):
        service.viability.create_policy({
            "crop_name": "水稻", "risk_level": "high", "interval_months": 12, "warning_days": 30,
            "minimum_germination_percent": 70, "effective_from": "2026-01-01", "effective_to": None,
            "created_by": "质量负责人",
        })
        completed_on = datetime.now(UTC) - timedelta(days=200)
        create_lot_with_completed_test(service, clock, "API1", 60, completed_on)
    created = client.post("/api/germplasm/policy-revisions", headers=headers, json={
        "crop_name": "水稻", "risk_level": "high", "interval_months": 1, "warning_days": 30,
        "minimum_germination_percent": 70, "effective_from": date.today().isoformat(),
        "change_note": "高风险作物复检间隔缩短", "created_by": "质量负责人",
    })
    assert created.status_code == 201, created.text
    revision_id = created.json()["id"]
    assert created.json()["status"] == "draft"

    denied = client.post(f"/api/germplasm/policy-revisions/{revision_id}/approve", headers=headers,
                         json={"actor": "质量负责人"})
    assert denied.status_code == 409  # 未预演不能审批

    preview = client.post(f"/api/germplasm/policy-revisions/{revision_id}/preview", headers=headers,
                          json={"actor": "质量负责人"})
    assert preview.status_code == 200, preview.text
    assert preview.json()["affected_lots"] == 1
    assert preview.json()["becomes_overdue_count"] == 1

    stored = client.get(f"/api/germplasm/policy-revisions/{revision_id}/preview", headers=headers)
    assert stored.status_code == 200
    assert stored.json()["items"][0]["lot_no"] == "LOT-API1"
    expected_due = add_months(completed_on.date(), 1).isoformat()
    assert stored.json()["items"][0]["new_due_on"] == expected_due

    conflicts = client.get(f"/api/germplasm/policy-revisions/{revision_id}/conflicts", headers=headers)
    assert conflicts.status_code == 200 and conflicts.json()["total"] == 0

    approved = client.post(f"/api/germplasm/policy-revisions/{revision_id}/approve", headers=headers,
                           json={"actor": "质量负责人"})
    assert approved.status_code == 200 and approved.json()["status"] == "approved"

    too_early = client.get(f"/api/germplasm/policy-revisions/{revision_id}/due-list", headers=headers)
    assert too_early.status_code == 409

    published = client.post(f"/api/germplasm/policy-revisions/{revision_id}/publish", headers=headers,
                            json={"actor": "质量负责人"})
    assert published.status_code == 200, published.text
    assert published.json()["publication"]["status"] == "completed"
    assert published.json()["action_counts"] == {"recomputed": 1}

    replay = client.post(f"/api/germplasm/policy-revisions/{revision_id}/publish", headers=headers,
                         json={"actor": "质量负责人"})
    assert replay.status_code == 200
    assert replay.json()["publication"]["processed_lots"] == 1

    due = client.get(f"/api/germplasm/policy-revisions/{revision_id}/due-list", headers=headers)
    assert due.status_code == 200
    assert due.json()["total"] == 1
    assert due.json()["items"][0]["effective_due_on"] == expected_due

    explanation = client.get("/api/germplasm/lots/1/retest-explanation", headers=headers)
    assert explanation.status_code == 200, explanation.text
    assert explanation.json()["policy"]["interval_months"] == 1
    assert explanation.json()["risk_level"] == "high"

    revisions = client.get("/api/germplasm/policy-revisions?crop_name=水稻&risk_level=high", headers=headers)
    assert revisions.status_code == 200
    assert [item["version"] for item in revisions.json()] == [2, 1]

    anonymous = client.get(f"/api/germplasm/policy-revisions/{revision_id}/preview")
    assert anonymous.status_code == 401
