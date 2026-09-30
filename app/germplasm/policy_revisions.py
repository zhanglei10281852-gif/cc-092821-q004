from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Any, Iterator

from app.core.clock import Clock, SystemClock, from_storage, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.germplasm.repository import GermplasmRepository, record, records
from app.germplasm.viability import add_months, risk_for_germination

# 与 viability.risk_for_germination 相同的分界，用于在 SQL 中筛选受影响批次
RISK_RANGES = {
    "high": "t.germination_percent < 70",
    "medium": "t.germination_percent >= 70 AND t.germination_percent < 85",
    "low": "t.germination_percent >= 85",
}

# 预演动作 → 发布动作 的对应关系；keep_* 表示需要人工关注的冲突批次
PLAN_ACTIONS = {"recompute", "confirm", "create", "keep_scheduled", "keep_waived"}
CONFLICT_ACTIONS = ("keep_scheduled", "keep_waived")


class PolicyRevisionService:
    """复检策略版本的可预演、可发布、可回滚治理流程。

    候选策略（draft）先预演得到受影响批次与新旧到期日，审批（approved）后发布：
    发布只重算尚未执行且未人工豁免的日程，按批次独立提交并记录游标，
    中断后再次调用 publish 即从断点继续；同一版本重复发布不会产生
    第二条有效日程或重复提醒。回滚通过复制历史版本生成新版本实现。
    """

    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        self.repository = GermplasmRepository(connection)

    # ------------------------------------------------------------------
    # 候选版本与回滚
    # ------------------------------------------------------------------
    def create_revision(self, data: dict[str, Any]) -> dict[str, Any]:
        timestamp = to_storage(self.clock.now())
        version = self._next_version(data["crop_name"], data["risk_level"])
        cursor = self.connection.execute(
            "INSERT INTO retest_policies(crop_name,risk_level,interval_months,warning_days,minimum_germination_percent,"
            "effective_from,effective_to,version,status,change_note,source_policy_id,created_by,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,'draft',?,?,?,?)",
            (
                data["crop_name"], data["risk_level"], data["interval_months"], data["warning_days"],
                data["minimum_germination_percent"], data["effective_from"], data.get("effective_to"), version,
                data.get("change_note", ""), data.get("source_policy_id"), data["created_by"], timestamp,
            ),
        )
        return self.repository.require_policy(int(cursor.lastrowid))

    def rollback(self, data: dict[str, Any]) -> dict[str, Any]:
        """回滚不改写历史版本，而是复制目标版本参数生成新的候选版本。"""
        target = record(self.connection.execute(
            "SELECT * FROM retest_policies WHERE crop_name=? AND risk_level=? AND version=?",
            (data["crop_name"], data["risk_level"], data["target_version"]),
        ).fetchone())
        if target is None:
            raise NotFoundError("目标策略版本不存在")
        note = data.get("change_note") or f"回滚至第 {target['version']} 版策略"
        return self.create_revision({
            "crop_name": target["crop_name"],
            "risk_level": target["risk_level"],
            "interval_months": target["interval_months"],
            "warning_days": target["warning_days"],
            "minimum_germination_percent": target["minimum_germination_percent"],
            "effective_from": data["effective_from"],
            "effective_to": data.get("effective_to"),
            "change_note": note,
            "source_policy_id": target["id"],
            "created_by": data["created_by"],
        })

    def list_revisions(
        self, crop_name: str | None, risk_level: str | None, status: str | None
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if crop_name:
            where.append("crop_name=?")
            params.append(crop_name)
        if risk_level:
            where.append("risk_level=?")
            params.append(risk_level)
        if status:
            where.append("status=?")
            params.append(status)
        clause = " WHERE " + " AND ".join(where) if where else ""
        return records(self.connection.execute(
            f"SELECT * FROM retest_policies{clause} ORDER BY crop_name,risk_level,version DESC", params
        ).fetchall())

    def revision_detail(self, revision_id: int) -> dict[str, Any]:
        revision = self.repository.require_policy(revision_id)
        detail = dict(revision)
        detail["preview"] = record(self.connection.execute(
            "SELECT * FROM retest_policy_previews WHERE policy_id=?", (revision_id,)
        ).fetchone())
        publication = self._publication_for(revision_id)
        detail["publication"] = publication
        if publication:
            detail["action_counts"] = self._action_counts(int(publication["id"]))
        return detail

    # ------------------------------------------------------------------
    # 预演
    # ------------------------------------------------------------------
    def preview(self, revision_id: int, data: dict[str, Any]) -> dict[str, Any]:
        revision = self.repository.require_policy(revision_id)
        if revision["status"] not in {"draft", "approved"}:
            raise ConflictError("只有候选或已审批的策略版本可以预演")
        today = self.clock.now().date()
        timestamp = to_storage(self.clock.now())
        # 预演可重复执行：每次重算并替换上一版预演结果
        self.connection.execute("DELETE FROM retest_policy_previews WHERE policy_id=?", (revision_id,))
        cursor = self.connection.execute(
            "INSERT INTO retest_policy_previews(policy_id,as_of,computed_by,computed_at) VALUES(?,?,?,?)",
            (revision_id, today.isoformat(), data["actor"], timestamp),
        )
        preview_id = int(cursor.lastrowid)
        counters = {
            "affected_lots": 0, "recompute_count": 0, "confirm_count": 0, "create_count": 0,
            "conflict_count": 0, "becomes_overdue_count": 0, "no_longer_overdue_count": 0, "notify_count": 0,
        }
        for row in self._scope_rows(revision):
            plan = self._plan_for_lot(revision, row, today)
            counters["affected_lots"] += 1
            if plan["action"] == "recompute":
                counters["recompute_count"] += 1
            elif plan["action"] == "confirm":
                counters["confirm_count"] += 1
            elif plan["action"] == "create":
                counters["create_count"] += 1
            if plan["action"] in CONFLICT_ACTIONS:
                counters["conflict_count"] += 1
            if plan["becomes_overdue"]:
                counters["becomes_overdue_count"] += 1
            if plan["no_longer_overdue"]:
                counters["no_longer_overdue_count"] += 1
            if plan["notify"]:
                counters["notify_count"] += 1
            self.connection.execute(
                "INSERT INTO retest_policy_preview_items(preview_id,lot_id,action,source_test_id,old_due_on,"
                "new_due_on,old_overdue,new_overdue,detail) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    preview_id, row["lot_id"], plan["action"], row["test_id"], plan["old_due_on"],
                    plan["new_due_on"], int(plan["old_overdue"]), int(plan["new_overdue"]), plan["detail"],
                ),
            )
        self.connection.execute(
            "UPDATE retest_policy_previews SET affected_lots=?,recompute_count=?,confirm_count=?,create_count=?,"
            "conflict_count=?,becomes_overdue_count=?,no_longer_overdue_count=?,notify_count=? WHERE id=?",
            (
                counters["affected_lots"], counters["recompute_count"], counters["confirm_count"],
                counters["create_count"], counters["conflict_count"], counters["becomes_overdue_count"],
                counters["no_longer_overdue_count"], counters["notify_count"], preview_id,
            ),
        )
        return self.get_preview(revision_id, limit=0, offset=0)["summary"]

    def get_preview(self, revision_id: int, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        self.repository.require_policy(revision_id)
        preview = record(self.connection.execute(
            "SELECT * FROM retest_policy_previews WHERE policy_id=?", (revision_id,)
        ).fetchone())
        if preview is None:
            raise NotFoundError("该策略版本尚未执行预演")
        total = int(self.connection.execute(
            "SELECT COUNT(*) FROM retest_policy_preview_items WHERE preview_id=?", (preview["id"],)
        ).fetchone()[0])
        items: list[dict[str, Any]] = []
        if limit:
            items = records(self.connection.execute(
                "SELECT i.*,l.lot_no,a.accession_no,a.crop_name FROM retest_policy_preview_items i "
                "JOIN seed_lots l ON l.id=i.lot_id JOIN accessions a ON a.id=l.accession_id "
                "WHERE i.preview_id=? ORDER BY i.lot_id LIMIT ? OFFSET ?",
                (preview["id"], limit, offset),
            ).fetchall())
        return {"summary": preview, "items": items, "total": total, "limit": limit, "offset": offset}

    def conflicts(self, revision_id: int) -> dict[str, Any]:
        self.repository.require_policy(revision_id)
        preview = record(self.connection.execute(
            "SELECT * FROM retest_policy_previews WHERE policy_id=?", (revision_id,)
        ).fetchone())
        if preview is None:
            raise NotFoundError("该策略版本尚未执行预演")
        placeholders = ",".join("?" for _ in CONFLICT_ACTIONS)
        items = records(self.connection.execute(
            f"SELECT i.*,l.lot_no,a.accession_no,a.crop_name FROM retest_policy_preview_items i "
            f"JOIN seed_lots l ON l.id=i.lot_id JOIN accessions a ON a.id=l.accession_id "
            f"WHERE i.preview_id=? AND i.action IN ({placeholders}) ORDER BY i.lot_id",
            (preview["id"], *CONFLICT_ACTIONS),
        ).fetchall())
        return {"preview_id": preview["id"], "as_of": preview["as_of"], "items": items, "total": len(items)}

    # ------------------------------------------------------------------
    # 审批与发布
    # ------------------------------------------------------------------
    def approve(self, revision_id: int, data: dict[str, Any]) -> dict[str, Any]:
        revision = self.repository.require_policy(revision_id)
        if revision["status"] != "draft":
            raise ConflictError("只有候选状态的策略版本可以审批")
        preview = record(self.connection.execute(
            "SELECT * FROM retest_policy_previews WHERE policy_id=?", (revision_id,)
        ).fetchone())
        if preview is None:
            raise ConflictError("请先执行预演再提交审批")
        timestamp = to_storage(self.clock.now())
        cursor = self.connection.execute(
            "UPDATE retest_policies SET status='approved',approved_by=?,approved_at=? WHERE id=? AND status='draft'",
            (data["actor"], timestamp, revision_id),
        )
        if cursor.rowcount != 1:
            raise ConflictError("策略版本状态已变化，无法审批")
        return self.repository.require_policy(revision_id)

    def publish(self, revision_id: int, data: dict[str, Any]) -> dict[str, Any]:
        actor = data["actor"]
        batch_size = data.get("batch_size")
        timestamp = to_storage(self.clock.now())
        with self._unit_of_work():
            revision = self.repository.require_policy(revision_id)
            publication = self._publication_for(revision_id)
            if revision["status"] == "published" and publication and publication["status"] == "completed":
                # 同一版本重复发布：直接返回既有结果，不重算、不重复提醒
                return self._publication_summary(revision_id)
            if revision["status"] != "approved":
                raise ConflictError("只有已审批的策略版本可以发布")
            newer = self.connection.execute(
                "SELECT version FROM retest_policies WHERE crop_name=? AND risk_level=? AND status='published' "
                "AND version>? ORDER BY version DESC LIMIT 1",
                (revision["crop_name"], revision["risk_level"], revision["version"]),
            ).fetchone()
            if newer:
                raise ConflictError(
                    "已存在更新的已发布策略版本，请基于最新版本重新发起",
                    context={"published_version": int(newer[0])},
                )
            if publication is None:
                cursor = self.connection.execute(
                    "INSERT INTO retest_policy_publications(policy_id,status,attempts,started_at,updated_at) "
                    "VALUES(?,'running',1,?,?)",
                    (revision_id, timestamp, timestamp),
                )
                publication_id = int(cursor.lastrowid)
            else:
                publication_id = int(publication["id"])
                self.connection.execute(
                    "UPDATE retest_policy_publications SET status='running',attempts=attempts+1,last_error=NULL,"
                    "updated_at=? WHERE id=?",
                    (timestamp, publication_id),
                )
            total = self._scope_count(revision)
            self.connection.execute(
                "UPDATE retest_policy_publications SET total_lots=? WHERE id=?", (total, publication_id)
            )
        try:
            exhausted = self._run_publication(revision_id, publication_id, batch_size)
        except Exception as exc:
            with self._unit_of_work():
                self.connection.execute(
                    "UPDATE retest_policy_publications SET status='failed',last_error=?,updated_at=? "
                    "WHERE id=? AND status='running'",
                    (str(exc)[:1000], to_storage(self.clock.now()), publication_id),
                )
            raise
        if exhausted:
            with self._unit_of_work():
                revision = self.repository.require_policy(revision_id)
                stamp = to_storage(self.clock.now())
                # 发布生效：取代同作物同风险的其他已发布版本（历史行保留，仅状态翻转）
                self.connection.execute(
                    "UPDATE retest_policies SET status='superseded',superseded_at=? "
                    "WHERE crop_name=? AND risk_level=? AND status='published' AND id<>?",
                    (stamp, revision["crop_name"], revision["risk_level"], revision_id),
                )
                self.connection.execute(
                    "UPDATE retest_policies SET status='published',published_by=?,published_at=? WHERE id=?",
                    (actor, stamp, revision_id),
                )
                self.connection.execute(
                    "UPDATE retest_policy_publications SET status='completed',finished_at=?,updated_at=? WHERE id=?",
                    (stamp, stamp, publication_id),
                )
        return self._publication_summary(revision_id)

    def due_list(self, revision_id: int, limit: int = 200, offset: int = 0) -> dict[str, Any]:
        """发布完成后的确定性到期列表：来自逐批次发布结果，顺序稳定。"""
        revision = self.repository.require_policy(revision_id)
        publication = self._publication_for(revision_id)
        if publication is None or publication["status"] != "completed":
            raise ConflictError("策略尚未发布完成，确定性到期列表不可用")
        total = int(self.connection.execute(
            "SELECT COUNT(*) FROM retest_policy_publication_items WHERE publication_id=?",
            (publication["id"],),
        ).fetchone()[0])
        items = records(self.connection.execute(
            "SELECT i.*,l.lot_no,a.accession_no,a.crop_name,"
            "CASE WHEN i.action IN ('kept_waived','kept_scheduled') THEN i.old_due_on ELSE i.new_due_on "
            "END AS effective_due_on "
            "FROM retest_policy_publication_items i "
            "JOIN seed_lots l ON l.id=i.lot_id JOIN accessions a ON a.id=l.accession_id "
            "WHERE i.publication_id=? ORDER BY effective_due_on,l.lot_no LIMIT ? OFFSET ?",
            (publication["id"], limit, offset),
        ).fetchall())
        return {
            "revision": revision,
            "publication": publication,
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    # ------------------------------------------------------------------
    # 时点解释
    # ------------------------------------------------------------------
    def explain_lot(self, lot_id: int, as_of: str | None = None) -> dict[str, Any]:
        lot = self.repository.require_lot(lot_id)
        accession = self.repository.require_accession(int(lot["accession_id"]))
        moment = self._parse_moment(as_of)
        moment_storage = to_storage(moment)
        on_date = moment.date().isoformat()
        test = self.repository.latest_completed_test(lot_id, moment_storage)
        risk = risk_for_germination(float(test["germination_percent"])) if test else None
        policy = (
            self.repository.policy_as_of(accession["crop_name"], risk, moment_storage, on_date) if risk else None
        )
        expected_due = None
        if test and policy:
            completed_date = from_storage(test["completed_at"]).date()  # type: ignore[union-attr]
            expected_due = add_months(completed_date, int(policy["interval_months"])).isoformat()
        schedule = record(self.connection.execute(
            "SELECT s.*,p.version AS policy_version,p.interval_months AS policy_interval_months,"
            "p.status AS policy_status FROM retest_schedules s JOIN retest_policies p ON p.id=s.policy_id "
            "WHERE s.lot_id=? ORDER BY CASE s.status WHEN 'pending' THEN 0 WHEN 'notified' THEN 1 "
            "WHEN 'scheduled' THEN 2 WHEN 'waived' THEN 3 ELSE 4 END,s.id DESC LIMIT 1",
            (lot_id,),
        ).fetchone())
        lines = [f"截至 {on_date}，批次 {lot['lot_no']} 属于作物 {accession['crop_name']}"]
        if test is None:
            lines.append("该时点之前没有已完成的有效检测，无法判定风险等级与适用策略")
        else:
            completed_date = from_storage(test["completed_at"]).date()  # type: ignore[union-attr]
            lines.append(
                f"最近有效检测 {test['test_no']} 于 {completed_date.isoformat()} 完成，"
                f"发芽率 {float(test['germination_percent']):.2f}%，对应风险等级 {risk}"
            )
            if policy is None:
                lines.append("该时点没有已发布且生效的适用策略版本")
            else:
                lines.append(
                    f"当时采用的策略为第 {policy['version']} 版（间隔 {policy['interval_months']} 个月，"
                    f"提前 {policy['warning_days']} 天预警，发布于 {policy['published_at']}）"
                )
                lines.append(f"按该策略应在 {expected_due} 前完成下一次复检")
        if schedule:
            lines.append(
                f"当前日程 #{schedule['id']} 到期日 {schedule['due_on']}，状态 {schedule['status']}，"
                f"依据策略第 {schedule['policy_version']} 版生成"
            )
        else:
            lines.append("当前没有复检日程")
        return {
            "lot_id": lot_id,
            "lot_no": lot["lot_no"],
            "as_of": moment_storage,
            "crop_name": accession["crop_name"],
            "risk_level": risk,
            "latest_valid_test": test,
            "policy": policy,
            "expected_due_on": expected_due,
            "current_schedule": schedule,
            "explanation": lines,
        }

    # ------------------------------------------------------------------
    # 内部：发布执行
    # ------------------------------------------------------------------
    def _run_publication(self, revision_id: int, publication_id: int, batch_size: int | None) -> bool:
        """逐批次处理，每批次一个事务并推进游标；返回 True 表示范围已处理完。"""
        processed = 0
        while True:
            with self._unit_of_work():
                revision = self.repository.require_policy(revision_id)
                publication = self._publication_for(revision_id)
                if publication is None or int(publication["id"]) != publication_id:
                    raise ConflictError("发布运行记录不存在")
                rows = self._scope_rows(revision, after_lot_id=int(publication["cursor_lot_id"]), limit=1)
                if not rows:
                    return True
                self._apply_to_lot(revision, publication, rows[0], self.clock.now().date())
                processed += 1
            if batch_size and processed >= batch_size:
                return False

    def _apply_to_lot(
        self, revision: dict[str, Any], publication: dict[str, Any], row: dict[str, Any], today: date
    ) -> None:
        timestamp = to_storage(self.clock.now())
        lot_id = int(row["lot_id"])
        test_id = int(row["test_id"])
        completed_date = from_storage(row["completed_at"]).date()  # type: ignore[union-attr]
        new_due = add_months(completed_date, int(revision["interval_months"]))
        new_due_iso = new_due.isoformat()
        active = self.repository.active_retest_schedule(lot_id)
        arranged = record(self.connection.execute(
            "SELECT * FROM retest_schedules WHERE lot_id=? AND status='scheduled' ORDER BY id DESC LIMIT 1",
            (lot_id,),
        ).fetchone())
        waived = record(self.connection.execute(
            "SELECT * FROM retest_schedules WHERE lot_id=? AND status='waived' ORDER BY id DESC LIMIT 1",
            (lot_id,),
        ).fetchone())
        old_due: str | None = None
        schedule_id: int | None = None
        if active:
            old_due = active["due_on"]
            schedule_id = int(active["id"])
            if active["due_on"] == new_due_iso and int(active["policy_id"]) == int(revision["id"]):
                action = "unchanged"  # 同一策略版本重复执行：已应用过，不再改动
            elif active["due_on"] == new_due_iso:
                self.connection.execute(
                    "UPDATE retest_schedules SET policy_id=?,source_test_id=?,reason=?,updated_at=? WHERE id=?",
                    (
                        revision["id"], test_id,
                        f"策略第 {revision['version']} 版发布复核，到期日维持不变", timestamp, active["id"],
                    ),
                )
                action = "confirmed"
            else:
                self.connection.execute(
                    "UPDATE retest_schedules SET status='superseded',updated_at=? WHERE id=?",
                    (timestamp, active["id"]),
                )
                schedule_id = self._insert_schedule(lot_id, test_id, revision, new_due_iso, timestamp)
                action = "recomputed"
        elif arranged:
            action, schedule_id, old_due = "kept_scheduled", int(arranged["id"]), arranged["due_on"]
        elif waived:
            action, schedule_id, old_due = "kept_waived", int(waived["id"]), waived["due_on"]
        else:
            schedule_id = self._insert_schedule(lot_id, test_id, revision, new_due_iso, timestamp)
            action = "created"
        notification_key: str | None = None
        notified = 0
        warning_horizon = (today + timedelta(days=int(revision["warning_days"]))).isoformat()
        if action in {"recomputed", "confirmed", "created", "unchanged"} and new_due_iso <= warning_horizon:
            notification_key = f"retest-reminder-{revision['id']}-{lot_id}"
            notified = self._enqueue_reminder(revision, lot_id, row["lot_no"], schedule_id, new_due_iso,
                                              notification_key, timestamp)
            self.connection.execute(
                "UPDATE retest_schedules SET status='notified',updated_at=? WHERE id=? AND status='pending'",
                (timestamp, schedule_id),
            )
        self.connection.execute(
            "INSERT INTO retest_policy_publication_items(publication_id,lot_id,action,schedule_id,old_due_on,"
            "new_due_on,notification_key,created_at) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(publication_id,lot_id) DO NOTHING",
            (publication["id"], lot_id, action, schedule_id, old_due, new_due_iso, notification_key, timestamp),
        )
        self.connection.execute(
            "UPDATE retest_policy_publications SET processed_lots=processed_lots+1,cursor_lot_id=?,"
            "notifications_enqueued=notifications_enqueued+?,updated_at=? WHERE id=?",
            (lot_id, notified, timestamp, publication["id"]),
        )

    def _insert_schedule(
        self, lot_id: int, test_id: int, revision: dict[str, Any], due_iso: str, timestamp: str
    ) -> int:
        reason = f"策略第 {revision['version']} 版发布重算"
        try:
            cursor = self.connection.execute(
                "INSERT INTO retest_schedules(lot_id,source_test_id,policy_id,due_on,status,reason,created_at,updated_at) "
                "VALUES(?,?,?,?,'pending',?,?,?)",
                (lot_id, test_id, revision["id"], due_iso, reason, timestamp, timestamp),
            )
            return int(cursor.lastrowid)
        except sqlite3.IntegrityError:
            # 同一到期日的历史日程（例如回滚后日期重合）恢复为待执行并刷新策略出处
            cursor = self.connection.execute(
                "UPDATE retest_schedules SET status='pending',source_test_id=?,policy_id=?,reason=?,"
                "waived_by=NULL,waived_at=NULL,waive_reason=NULL,updated_at=? "
                "WHERE lot_id=? AND due_on=? AND status='superseded'",
                (test_id, revision["id"], reason, timestamp, lot_id, due_iso),
            )
            if cursor.rowcount != 1:
                raise ConflictError(
                    "复检日程到期日冲突且无法恢复", context={"lot_id": lot_id, "due_on": due_iso}
                )
            row = self.connection.execute(
                "SELECT id FROM retest_schedules WHERE lot_id=? AND due_on=?", (lot_id, due_iso)
            ).fetchone()
            return int(row[0])

    def _enqueue_reminder(
        self,
        revision: dict[str, Any],
        lot_id: int,
        lot_no: str,
        schedule_id: int | None,
        due_iso: str,
        event_key: str,
        timestamp: str,
    ) -> int:
        payload = {
            "lot_id": lot_id,
            "lot_no": lot_no,
            "due_on": due_iso,
            "policy_id": revision["id"],
            "policy_version": revision["version"],
            "crop_name": revision["crop_name"],
            "risk_level": revision["risk_level"],
        }
        cursor = self.connection.execute(
            "INSERT OR IGNORE INTO outbox_events(event_key,event_type,aggregate_type,aggregate_id,payload_json,"
            "available_at,created_at) VALUES(?,?,?,?,?,?,?)",
            (
                event_key, "retest.schedule.reminder", "retest_schedule", str(schedule_id),
                json.dumps(payload, ensure_ascii=False, sort_keys=True), timestamp, timestamp,
            ),
        )
        return int(cursor.rowcount)

    # ------------------------------------------------------------------
    # 内部：预演计划
    # ------------------------------------------------------------------
    def _plan_for_lot(self, revision: dict[str, Any], row: dict[str, Any], today: date) -> dict[str, Any]:
        lot_id = int(row["lot_id"])
        today_iso = today.isoformat()
        completed_date = from_storage(row["completed_at"]).date()  # type: ignore[union-attr]
        new_due = add_months(completed_date, int(revision["interval_months"]))
        new_due_iso = new_due.isoformat()
        active = self.repository.active_retest_schedule(lot_id)
        arranged = record(self.connection.execute(
            "SELECT * FROM retest_schedules WHERE lot_id=? AND status='scheduled' ORDER BY id DESC LIMIT 1",
            (lot_id,),
        ).fetchone())
        waived = record(self.connection.execute(
            "SELECT * FROM retest_schedules WHERE lot_id=? AND status='waived' ORDER BY id DESC LIMIT 1",
            (lot_id,),
        ).fetchone())
        old_due: str | None = None
        if active:
            old_due = active["due_on"]
            if active["due_on"] == new_due_iso:
                action = "confirm"
                detail = f"到期日维持 {new_due_iso}，策略出处更新为第 {revision['version']} 版"
            else:
                action = "recompute"
                detail = f"到期日 {old_due} → {new_due_iso}"
        elif arranged:
            action, old_due = "keep_scheduled", arranged["due_on"]
            detail = "已安排检测，保留现状，待检测完成后按新策略生成日程"
        elif waived:
            action, old_due = "keep_waived", waived["due_on"]
            detail = "人工豁免日程，保留不重算"
        else:
            action = "create"
            detail = f"新增到期日 {new_due_iso}"
        effective_new = new_due_iso if action in {"recompute", "confirm", "create"} else old_due
        old_overdue = bool(old_due) and str(old_due) < today_iso
        new_overdue = bool(effective_new) and str(effective_new) < today_iso
        warning_horizon = (today + timedelta(days=int(revision["warning_days"]))).isoformat()
        return {
            "action": action,
            "old_due_on": old_due,
            "new_due_on": new_due_iso,
            "old_overdue": old_overdue,
            "new_overdue": new_overdue,
            "becomes_overdue": not old_overdue and new_overdue,
            "no_longer_overdue": old_overdue and not new_overdue,
            "notify": action in {"recompute", "confirm", "create"} and new_due_iso <= warning_horizon,
            "detail": detail,
        }

    # ------------------------------------------------------------------
    # 内部：通用
    # ------------------------------------------------------------------
    def _scope_rows(
        self, revision: dict[str, Any], after_lot_id: int = 0, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """受影响批次：同作物、未退出保存循环、最近有效检测落在该策略风险档。"""
        sql = (
            "SELECT l.id AS lot_id,l.lot_no,t.id AS test_id,t.completed_at,t.germination_percent "
            + self._scope_from_where(revision)
            + " AND l.id>? ORDER BY l.id"
        )
        params: list[Any] = [revision["crop_name"], after_lot_id]
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        return records(self.connection.execute(sql, params).fetchall())

    def _scope_count(self, revision: dict[str, Any]) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) " + self._scope_from_where(revision), (revision["crop_name"],)
        ).fetchone()
        return int(row[0])

    def _scope_from_where(self, revision: dict[str, Any]) -> str:
        return (
            "FROM seed_lots l JOIN accessions a ON a.id=l.accession_id "
            "JOIN viability_tests t ON t.id=(SELECT id FROM viability_tests WHERE lot_id=l.id "
            "AND status='completed' ORDER BY completed_at DESC,id DESC LIMIT 1) "
            f"WHERE a.crop_name=? AND l.status NOT IN ('depleted','disposed') "
            f"AND {RISK_RANGES[revision['risk_level']]}"
        )

    def _next_version(self, crop_name: str, risk_level: str) -> int:
        latest = self.connection.execute(
            "SELECT version FROM retest_policies WHERE crop_name=? AND risk_level=? ORDER BY version DESC LIMIT 1",
            (crop_name, risk_level),
        ).fetchone()
        return int(latest[0]) + 1 if latest else 1

    def _publication_for(self, revision_id: int) -> dict[str, Any] | None:
        return record(self.connection.execute(
            "SELECT * FROM retest_policy_publications WHERE policy_id=?", (revision_id,)
        ).fetchone())

    def _action_counts(self, publication_id: int) -> dict[str, int]:
        rows = self.connection.execute(
            "SELECT action,COUNT(*) AS c FROM retest_policy_publication_items WHERE publication_id=? "
            "GROUP BY action ORDER BY action",
            (publication_id,),
        ).fetchall()
        return {str(row["action"]): int(row["c"]) for row in rows}

    def _publication_summary(self, revision_id: int) -> dict[str, Any]:
        revision = self.repository.require_policy(revision_id)
        publication = self._publication_for(revision_id)
        return {
            "revision": revision,
            "publication": publication,
            "action_counts": self._action_counts(int(publication["id"])) if publication else {},
        }

    def _parse_moment(self, raw: str | None) -> datetime:
        if not raw:
            return self.clock.now()
        text = raw.strip()
        try:
            if len(text) == 10:
                parsed = datetime.fromisoformat(text).replace(hour=23, minute=59, second=59)
            else:
                parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValidationError("as_of 必须是 ISO 格式的日期或时间") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed

    @contextmanager
    def _unit_of_work(self) -> Iterator[None]:
        """发布按批次独立提交；被外层事务包裹时退化为保存点，保持逐批次原子性。"""
        if self.connection.in_transaction:
            marker = f"policy_revision_{id(object())}"
            self.connection.execute(f"SAVEPOINT {marker}")
            try:
                yield
                self.connection.execute(f"RELEASE SAVEPOINT {marker}")
            except Exception:
                self.connection.execute(f"ROLLBACK TO SAVEPOINT {marker}")
                self.connection.execute(f"RELEASE SAVEPOINT {marker}")
                raise
            return
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
