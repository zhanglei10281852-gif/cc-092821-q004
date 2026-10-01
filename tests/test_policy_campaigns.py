from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.core.clock import FrozenClock
from app.core.errors import ConflictError
from app.database import get_connection, transaction
from app.germplasm.service import GermplasmService
from app.germplasm.viability import add_months

START = datetime(2026, 1, 15, 8, 0, tzinfo=UTC)
PUBLISH_DAY = datetime(2026, 9, 30, 8, 0, tzinfo=UTC)


def build_lot_with_completed_test(
    service: GermplasmService,
    suffix: str,
    normal: int = 60,
    interval_months: int = 12,
) -> dict:
    """建立批次并完成一次发芽检测，按 12 个月旧策略生成待执行复检日程。"""
    source = service.accessions.create_source({
        "source_code": f"SRC-{suffix}", "provider_name": "省级采集队", "country_code": "CN",
        "locality": "河谷试验站", "collected_on": "2025-10-02", "permit_reference": None,
        "restrictions": {},
    })
    accession = service.accessions.create_accession({
        "accession_no": f"ACC-{suffix}", "scientific_name": "Oryza sativa", "crop_name": "水稻",
        "cultivar_name": "地方材料", "source_id": source["id"], "acquisition_type": "采集",
        "received_on": "2026-01-02", "passport": {}, "created_by": "登记员",
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
        "treatment": "清选干燥", "sealed_on": "2026-01-03", "created_by": "登记员",
    })
    service.inventory.place_lot({
        "lot_id": lot["id"], "location_id": location["id"], "weight_grams": 500,
        "container_code": f"BOX-{suffix}", "idempotency_key": f"place-{suffix}-0001", "actor": "保管员",
    })
    protocol = service.viability.create_protocol({
        "protocol_code": f"GER-{suffix}", "crop_name": "水稻", "sample_size": 100, "replicate_count": 2,
        "temperature_c": 25, "duration_days": 14, "normal_seedling_rule": "根芽发育完整",
        "created_by": "技术负责人",
    })
    service.viability.create_policy({
        "crop_name": "水稻", "risk_level": "high", "interval_months": interval_months, "warning_days": 30,
        "minimum_germination_percent": 70, "effective_from": "2026-01-01", "effective_to": None,
        "created_by": "质量负责人",
    })
    test = service.viability.schedule_test({
        "test_no": f"VT-{suffix}", "lot_id": lot["id"], "protocol_id": protocol["id"], "test_type": "周期复检",
        "sampled_grams": 5, "scheduled_for": "2026-01-15", "requested_by": "检测员",
        "idempotency_key": f"schedule-{suffix}-0001",
    })
    service.viability.start_test(test["id"], {"performed_by": "检测员", "expected_version": 1})
    for replicate in (1, 2):
        service.viability.add_count(test["id"], {
            "replicate_no": replicate, "seeds_tested": 100, "normal_count": normal,
            "abnormal_count": 10, "dead_count": 100 - normal - 10, "fresh_count": 0,
            "observation_day": 14, "observed_by": "检测员",
        })
    completed = service.viability.complete_test(test["id"], {"performed_by": "检测员", "expected_version": 2})
    return {"lot": service.repository.require_lot(lot["id"]), "test": completed, "protocol": protocol}


def make_campaign(service: GermplasmService, interval_months: int = 6) -> dict:
    return service.policy_campaigns.create_campaign({
        "crop_name": "水稻", "risk_level": "high", "interval_months": interval_months,
        "warning_days": 30, "minimum_germination_percent": 70, "effective_from": "2026-09-30",
        "note": "高风险作物复检间隔缩短", "created_by": "质量负责人",
    })


def active_schedules(connection, lot_id: int) -> list[dict]:
    rows = connection.execute(
        "SELECT * FROM retest_schedules WHERE lot_id=? AND status IN ('pending','notified') ORDER BY due_on",
        (lot_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def recalc_events(connection) -> list[dict]:
    rows = connection.execute(
        "SELECT * FROM outbox_events WHERE event_type='retest_schedule_recalculated' ORDER BY id"
    ).fetchall()
    return [dict(row) for row in rows]


def test_preview_computes_impacted_lots_and_overdue_changes(client):
    clock = FrozenClock(START)
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection, clock)
        built = build_lot_with_completed_test(service, "P01")
        clock.current = PUBLISH_DAY
        campaign = make_campaign(service)
        assert campaign["status"] == "draft"

        detail = service.policy_campaigns.preview(campaign["id"])
        summary = detail["summary"]
        assert detail["status"] == "previewed"
        assert summary["affected_lots"] == 1
        assert summary["to_recalculate"] == 1
        assert summary["moved_earlier"] == 1
        assert summary["newly_overdue"] == 1
        assert summary["current_policy"]["interval_months"] == 12
        assert summary["candidate_interval_months"] == 6

        items = service.policy_campaigns.list_items(campaign["id"])
        assert len(items) == 1
        item = items[0]
        assert item["lot_id"] == built["lot"]["id"]
        assert item["old_due_on"] == "2027-01-15"
        assert item["new_due_on"] == "2026-07-15"
        assert item["change_type"] == "earlier"
        assert item["overdue_change"] == "newly_overdue"
        assert item["conflict"] is None


def test_publish_recalculates_only_open_schedules_and_keeps_history(client):
    clock = FrozenClock(START)
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection, clock)
        built = build_lot_with_completed_test(service, "P02")
        lot_id = built["lot"]["id"]
        old_schedule = active_schedules(connection, lot_id)[0]
        clock.current = PUBLISH_DAY
        campaign = make_campaign(service)
        service.policy_campaigns.preview(campaign["id"])
        service.policy_campaigns.approve(campaign["id"], "质量负责人")
        published = service.policy_campaigns.publish(campaign["id"], "质量负责人")
        assert published["status"] == "published"

        candidate = service.repository.require_policy(campaign["policy_id"])
        assert candidate["status"] == "published"
        assert candidate["version"] == 2

        detail = service.policy_campaigns.apply(campaign["id"])
        assert detail["status"] == "applied"
        assert detail["progress"] == {"total": 1, "pending": 0, "done": 1, "skipped": 0, "failed": 0}

        new_active = active_schedules(connection, lot_id)
        assert len(new_active) == 1
        assert new_active[0]["due_on"] == "2026-07-15"
        assert new_active[0]["policy_id"] == campaign["policy_id"]

        history = connection.execute(
            "SELECT * FROM retest_schedules WHERE id=?", (old_schedule["id"],)
        ).fetchone()
        assert history["status"] == "superseded"
        assert history["policy_id"] == old_schedule["policy_id"]
        assert history["due_on"] == "2027-01-15"

        test = service.repository.require_test(built["test"]["id"])
        assert test["status"] == "completed"
        assert test["germination_percent"] == 60

        events = recalc_events(connection)
        assert len(events) == 1
        assert events[0]["event_key"] == f"retest-recalc:{campaign['id']}:{lot_id}"


def test_apply_is_idempotent_and_resumable(client, monkeypatch):
    clock = FrozenClock(START)
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection, clock)
        first = build_lot_with_completed_test(service, "R01")
        second = build_lot_with_completed_test(service, "R02")
        clock.current = PUBLISH_DAY
        campaign = make_campaign(service)
        service.policy_campaigns.preview(campaign["id"])
        service.policy_campaigns.approve(campaign["id"], "质量负责人")
        service.policy_campaigns.publish(campaign["id"], "质量负责人")
        campaign_id = campaign["id"]

    # 事务外执行：模拟第一条明细提交后进程中断
    runner = GermplasmService(get_connection(), clock)
    original = runner.policy_campaigns._apply_item
    calls = {"count": 0}

    def flaky(campaign, item):
        calls["count"] += 1
        if calls["count"] == 2:
            raise KeyboardInterrupt("模拟进程中断")
        return original(campaign, item)

    monkeypatch.setattr(runner.policy_campaigns, "_apply_item", flaky)
    with pytest.raises(KeyboardInterrupt):
        runner.policy_campaigns.apply(campaign_id)

    connection = get_connection()
    done = connection.execute(
        "SELECT COUNT(*) FROM retest_policy_campaign_items WHERE campaign_id=? AND status='done'",
        (campaign_id,),
    ).fetchone()[0]
    pending = connection.execute(
        "SELECT COUNT(*) FROM retest_policy_campaign_items WHERE campaign_id=? AND status='pending'",
        (campaign_id,),
    ).fetchone()[0]
    assert done == 1 and pending == 1
    assert len(recalc_events(connection)) == 1

    # 从中断处恢复：只处理剩余明细，不重复生成日程和提醒
    resumed = GermplasmService(get_connection(), clock).policy_campaigns.apply(campaign_id)
    assert resumed["status"] == "applied"
    assert resumed["progress"]["done"] == 2

    again = GermplasmService(get_connection(), clock).policy_campaigns.apply(campaign_id)
    assert again["status"] == "applied"

    for built in (first, second):
        assert len(active_schedules(connection, built["lot"]["id"])) == 1
    total_schedules = connection.execute(
        "SELECT COUNT(*) FROM retest_schedules WHERE status IN ('pending','notified')"
    ).fetchone()[0]
    assert total_schedules == 2
    assert len(recalc_events(connection)) == 2


def test_waived_and_scheduled_lots_are_conflicts_and_not_recalculated(client):
    clock = FrozenClock(START)
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection, clock)
        waived = build_lot_with_completed_test(service, "W01")
        scheduled = build_lot_with_completed_test(service, "W02")
        normal = build_lot_with_completed_test(service, "W03")

        waived_schedule = active_schedules(connection, waived["lot"]["id"])[0]
        service.policy_campaigns.waive_schedule(waived_schedule["id"], "质量负责人", "种质已另行安排复壮")

        protocol = service.repository.protocol_latest("GER-W02")
        service.viability.schedule_test({
            "test_no": "VT-W02-B", "lot_id": scheduled["lot"]["id"], "protocol_id": protocol["id"],
            "test_type": "周期复检", "sampled_grams": 5, "scheduled_for": "2026-10-10",
            "requested_by": "检测员", "idempotency_key": "schedule-w02-b0001",
        })

        clock.current = PUBLISH_DAY
        campaign = make_campaign(service)
        detail = service.policy_campaigns.preview(campaign["id"])
        assert detail["summary"]["conflicts"] == 2
        assert detail["summary"]["to_recalculate"] == 1
        conflict_reasons = {item["lot_no"]: item["conflict"] for item in detail["conflicts"]}
        assert conflict_reasons == {"LOT-W01": "manually_waived", "LOT-W02": "already_scheduled"}

        service.policy_campaigns.approve(campaign["id"], "质量负责人")
        service.policy_campaigns.publish(campaign["id"], "质量负责人")
        applied = service.policy_campaigns.apply(campaign["id"])
        assert applied["status"] == "applied"
        assert applied["progress"]["skipped"] == 2
        assert applied["progress"]["done"] == 1

        waived_row = connection.execute(
            "SELECT * FROM retest_schedules WHERE id=?", (waived_schedule["id"],)
        ).fetchone()
        assert waived_row["status"] == "waived"
        assert waived_row["waived_by"] == "质量负责人"
        assert active_schedules(connection, waived["lot"]["id"]) == []
        assert len(active_schedules(connection, normal["lot"]["id"])) == 1
        assert len(recalc_events(connection)) == 1


def test_rollback_creates_new_version_without_rewriting_history(client):
    clock = FrozenClock(START)
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection, clock)
        built = build_lot_with_completed_test(service, "B01")
        lot_id = built["lot"]["id"]
        clock.current = PUBLISH_DAY
        campaign = make_campaign(service)
        service.policy_campaigns.preview(campaign["id"])
        service.policy_campaigns.approve(campaign["id"], "质量负责人")
        service.policy_campaigns.publish(campaign["id"], "质量负责人")
        service.policy_campaigns.apply(campaign["id"])

        rollback = service.policy_campaigns.rollback(campaign["id"], "质量负责人")
        assert rollback["status"] == "draft"
        assert rollback["rollback_of"] == campaign["id"]
        assert rollback["interval_months"] == 12
        rollback_policy = service.repository.require_policy(rollback["policy_id"])
        assert rollback_policy["version"] == 3
        assert rollback_policy["status"] == "candidate"

        # 旧策略版本记录保持原样
        versions = connection.execute(
            "SELECT version,status,interval_months,published_at FROM retest_policies "
            "WHERE crop_name='水稻' AND risk_level='high' ORDER BY version"
        ).fetchall()
        assert [(row["version"], row["status"], row["interval_months"]) for row in versions] == [
            (1, "published", 12), (2, "published", 6), (3, "candidate", 12),
        ]
        assert versions[0]["published_at"] is not None
        assert versions[1]["published_at"] is not None

        service.policy_campaigns.preview(rollback["id"])
        service.policy_campaigns.approve(rollback["id"], "质量负责人")
        service.policy_campaigns.publish(rollback["id"], "质量负责人")
        service.policy_campaigns.apply(rollback["id"])

        current = active_schedules(connection, lot_id)
        assert len(current) == 1
        assert current[0]["due_on"] == "2027-01-15"
        assert current[0]["policy_id"] == rollback["policy_id"]
        applicable = service.repository.applicable_policy("水稻", "high", "2026-09-30")
        assert applicable["version"] == 3
        assert applicable["interval_months"] == 12


def test_explain_lot_uses_point_in_time_policy_version(client):
    clock = FrozenClock(START)
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection, clock)
        built = build_lot_with_completed_test(service, "E01")
        lot_id = built["lot"]["id"]
        clock.current = PUBLISH_DAY
        campaign = make_campaign(service)
        service.policy_campaigns.preview(campaign["id"])
        service.policy_campaigns.approve(campaign["id"], "质量负责人")
        service.policy_campaigns.publish(campaign["id"], "质量负责人")
        service.policy_campaigns.apply(campaign["id"])

        before = service.policy_campaigns.explain_lot(lot_id, datetime(2026, 3, 1, tzinfo=UTC))
        assert before["crop_name"] == "水稻"
        assert before["risk_level"] == "high"
        assert before["latest_test"]["germination_percent"] == 60
        assert before["policy"]["version"] == 1
        assert before["policy"]["interval_months"] == 12
        assert before["due_on"] == "2027-01-15"

        after = service.policy_campaigns.explain_lot(lot_id, datetime(2026, 10, 1, tzinfo=UTC))
        assert after["policy"]["version"] == 2
        assert after["policy"]["interval_months"] == 6
        assert after["due_on"] == "2026-07-15"
        assert after["current_schedule"]["policy_id"] == campaign["policy_id"]

        too_early = service.policy_campaigns.explain_lot(lot_id, datetime(2025, 12, 1, tzinfo=UTC))
        assert too_early["latest_test"] is None
        assert too_early["policy"] is None


def test_campaign_requires_preview_before_approve_and_blocks_parallel_candidates(client):
    clock = FrozenClock(START)
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection, clock)
        build_lot_with_completed_test(service, "G01")
        clock.current = PUBLISH_DAY
        campaign = make_campaign(service)
        with pytest.raises(ConflictError):
            service.policy_campaigns.approve(campaign["id"], "质量负责人")
        with pytest.raises(ConflictError):
            service.policy_campaigns.publish(campaign["id"], "质量负责人")
        with pytest.raises(ConflictError):
            service.policy_campaigns.apply(campaign["id"])
        with pytest.raises(ConflictError):
            make_campaign(service)
        service.policy_campaigns.preview(campaign["id"])
        service.policy_campaigns.approve(campaign["id"], "质量负责人")
        with pytest.raises(ConflictError):
            service.policy_campaigns.preview(campaign["id"])


def test_unchanged_interval_is_skipped_without_notification(client):
    clock = FrozenClock(START)
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection, clock)
        built = build_lot_with_completed_test(service, "U01")
        clock.current = PUBLISH_DAY
        campaign = make_campaign(service, interval_months=12)
        detail = service.policy_campaigns.preview(campaign["id"])
        assert detail["summary"]["unchanged"] == 1
        assert detail["summary"]["to_recalculate"] == 0
        service.policy_campaigns.approve(campaign["id"], "质量负责人")
        service.policy_campaigns.publish(campaign["id"], "质量负责人")
        applied = service.policy_campaigns.apply(campaign["id"])
        assert applied["progress"]["skipped"] == 1
        schedules = active_schedules(connection, built["lot"]["id"])
        assert len(schedules) == 1
        assert schedules[0]["due_on"] == "2027-01-15"
        assert recalc_events(connection) == []


def test_api_campaign_flow_and_deterministic_schedule_list(client, admin):
    headers = admin["headers"]
    source = client.post("/api/germplasm/sources", headers=headers, json={
        "source_code": "API-SRC-1", "provider_name": "合作站", "country_code": "CN", "restrictions": {},
    })
    accession = client.post("/api/germplasm/accessions", headers=headers, json={
        "accession_no": "API-ACC-1", "scientific_name": "Oryza sativa", "crop_name": "水稻",
        "source_id": source.json()["id"], "acquisition_type": "采集",
        "received_on": "2026-09-01", "passport": {}, "created_by": "登记员",
    })
    client.post(f"/api/germplasm/accessions/{accession.json()['id']}/transition", headers=headers, json={
        "target_status": "accepted", "reason": "手续齐全", "expected_version": 1, "actor": "审核员",
    })
    location = client.post("/api/germplasm/locations", headers=headers, json={
        "location_code": "API-L1", "facility": "长期库", "room": "一室", "rack": "A", "shelf": "1",
        "capacity_grams": 3000, "temperature_c": -18, "humidity_percent": 30,
    })
    lot = client.post("/api/germplasm/lots", headers=headers, json={
        "lot_no": "API-LOT-1", "accession_id": accession.json()["id"], "harvest_year": 2025,
        "initial_weight_grams": 800, "moisture_percent": 8, "treatment": "清选", "created_by": "登记员",
    })
    client.post("/api/germplasm/placements", headers=headers, json={
        "lot_id": lot.json()["id"], "location_id": location.json()["id"], "weight_grams": 800,
        "container_code": "API-BOX-1", "idempotency_key": "api-place-00001", "actor": "保管员",
    })
    protocol = client.post("/api/germplasm/protocols", headers=headers, json={
        "protocol_code": "API-GER", "crop_name": "水稻", "sample_size": 100, "replicate_count": 2,
        "temperature_c": 25, "duration_days": 14, "normal_seedling_rule": "根芽发育完整",
        "created_by": "技术负责人",
    })
    client.post("/api/germplasm/policies", headers=headers, json={
        "crop_name": "水稻", "risk_level": "high", "interval_months": 12, "warning_days": 30,
        "minimum_germination_percent": 70, "effective_from": "2026-01-01", "created_by": "质量负责人",
    })
    test = client.post("/api/germplasm/tests", headers=headers, json={
        "test_no": "API-VT-1", "lot_id": lot.json()["id"], "protocol_id": protocol.json()["id"],
        "test_type": "周期复检", "sampled_grams": 5, "scheduled_for": "2026-09-20",
        "requested_by": "检测员", "idempotency_key": "api-schedule-001",
    })
    assert test.status_code == 201, test.text
    client.post(f"/api/germplasm/tests/{test.json()['id']}/start", headers=headers, json={
        "performed_by": "检测员", "expected_version": 1,
    })
    for replicate in (1, 2):
        counted = client.post(f"/api/germplasm/tests/{test.json()['id']}/counts", headers=headers, json={
            "replicate_no": replicate, "seeds_tested": 100, "normal_count": 60,
            "abnormal_count": 10, "dead_count": 30, "fresh_count": 0,
            "observation_day": 14, "observed_by": "检测员",
        })
        assert counted.status_code == 201, counted.text
    completed = client.post(f"/api/germplasm/tests/{test.json()['id']}/complete", headers=headers, json={
        "performed_by": "检测员", "expected_version": 2,
    })
    assert completed.status_code == 200, completed.text

    campaign = client.post("/api/germplasm/policy-campaigns", headers=headers, json={
        "crop_name": "水稻", "risk_level": "high", "interval_months": 6, "warning_days": 30,
        "minimum_germination_percent": 70, "effective_from": "2026-09-30",
        "note": "高风险作物复检间隔缩短为六个月", "created_by": "质量负责人",
    })
    assert campaign.status_code == 201, campaign.text
    campaign_id = campaign.json()["id"]

    preview = client.post(f"/api/germplasm/policy-campaigns/{campaign_id}/preview", headers=headers)
    assert preview.status_code == 200, preview.text
    assert preview.json()["summary"]["to_recalculate"] == 1
    assert preview.json()["summary"]["current_policy"]["interval_months"] == 12

    detail = client.get(f"/api/germplasm/policy-campaigns/{campaign_id}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["conflicts"] == []
    items = client.get(f"/api/germplasm/policy-campaigns/{campaign_id}/items", headers=headers)
    assert items.status_code == 200
    assert items.json()[0]["old_due_on"] > items.json()[0]["new_due_on"]

    approved = client.post(f"/api/germplasm/policy-campaigns/{campaign_id}/approve", headers=headers, json={
        "actor": "质量负责人",
    })
    assert approved.status_code == 200, approved.text
    published = client.post(f"/api/germplasm/policy-campaigns/{campaign_id}/publish", headers=headers, json={
        "actor": "质量负责人",
    })
    assert published.status_code == 200, published.text

    applied = client.post(f"/api/germplasm/policy-campaigns/{campaign_id}/apply", headers=headers)
    assert applied.status_code == 200, applied.text
    assert applied.json()["status"] == "applied"
    assert applied.json()["progress"]["done"] == 1

    schedules = client.get(f"/api/germplasm/policy-campaigns/{campaign_id}/schedules", headers=headers)
    assert schedules.status_code == 200
    assert len(schedules.json()) == 1
    entry = schedules.json()[0]
    assert entry["lot_no"] == "API-LOT-1"
    assert entry["status"] == "pending"
    assert entry["policy_id"] == campaign.json()["policy_id"]

    explanation = client.get(f"/api/germplasm/lots/{lot.json()['id']}/retest-explanation", headers=headers)
    assert explanation.status_code == 200
    body = explanation.json()
    assert body["crop_name"] == "水稻"
    assert body["risk_level"] == "high"
    assert body["policy"]["version"] == 2
    assert body["policy"]["interval_months"] == 6
    expected_due = add_months(datetime.now(UTC).date(), 6).isoformat()
    assert body["due_on"] == expected_due

    rollback = client.post(f"/api/germplasm/policy-campaigns/{campaign_id}/rollback", headers=headers, json={
        "actor": "质量负责人",
    })
    assert rollback.status_code == 201, rollback.text
    assert rollback.json()["interval_months"] == 12
    assert rollback.json()["rollback_of"] == campaign_id


def test_api_campaign_requires_authentication(client):
    response = client.post("/api/germplasm/policy-campaigns", json={
        "crop_name": "水稻", "risk_level": "high", "interval_months": 6, "warning_days": 30,
        "minimum_germination_percent": 70, "effective_from": "2026-09-30", "created_by": "质量负责人",
    })
    assert response.status_code == 401
