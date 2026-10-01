from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from typing import Any

from app.core.clock import Clock, SystemClock, from_storage, to_storage
from app.core.errors import ConflictError, NotFoundError
from app.database import transaction
from app.germplasm.repository import GermplasmRepository, record, records
from app.germplasm.viability import add_months, risk_for_germination

# 待重算的日程状态：尚未执行（pending/notified）；scheduled 视为已执行，waived 视为人工豁免
OPEN_SCHEDULE_STATUSES = ("pending", "notified")
# 同一作物和风险等级只允许一个未发布的升级活动，避免候选版本号撞车
IN_FLIGHT_STATUSES = ("draft", "previewed", "approved")

_CONFLICT_LABELS = {
    "already_scheduled": "已安排检测任务，属于已执行日程",
    "manually_waived": "已人工豁免",
    "no_active_schedule": "没有待执行的复检日程",
}


class PolicyCampaignService:
    """复检策略升级的版本化流程：候选策略 -> 预演 -> 审批 -> 发布 -> 断点可恢复的重算。"""

    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        self.repository = GermplasmRepository(connection)

    # ---------------------------------------------------------------- 创建与回滚

    def create_campaign(self, data: dict[str, Any]) -> dict[str, Any]:
        crop = str(data["crop_name"]).strip()
        risk = data["risk_level"]
        in_flight = self.connection.execute(
            "SELECT id FROM retest_policy_campaigns WHERE crop_name=? AND risk_level=? AND status IN "
            "('draft','previewed','approved')",
            (crop, risk),
        ).fetchone()
        if in_flight:
            raise ConflictError("同一作物和风险等级已有进行中的升级活动", context={"campaign_id": in_flight[0]})
        latest = self.connection.execute(
            "SELECT version FROM retest_policies WHERE crop_name=? AND risk_level=? ORDER BY version DESC LIMIT 1",
            (crop, risk),
        ).fetchone()
        version = int(latest[0]) + 1 if latest else 1
        timestamp = to_storage(self.clock.now())
        cursor = self.connection.execute(
            "INSERT INTO retest_policies(crop_name,risk_level,interval_months,warning_days,minimum_germination_percent,"
            "effective_from,effective_to,version,status,published_at,created_by,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,'candidate',NULL,?,?)",
            (
                crop, risk, data["interval_months"], data["warning_days"], data["minimum_germination_percent"],
                data["effective_from"], None, version, data["created_by"], timestamp,
            ),
        )
        policy_id = int(cursor.lastrowid)
        campaign_no = f"PC-{crop}-{risk}-V{version}"
        cursor = self.connection.execute(
            "INSERT INTO retest_policy_campaigns(campaign_no,crop_name,risk_level,interval_months,warning_days,"
            "minimum_germination_percent,effective_from,note,policy_id,rollback_of,status,created_by,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,'draft',?,?,?)",
            (
                campaign_no, crop, risk, data["interval_months"], data["warning_days"],
                data["minimum_germination_percent"], data["effective_from"], data.get("note", ""),
                policy_id, data.get("rollback_of"), data["created_by"], timestamp, timestamp,
            ),
        )
        return self.campaign_detail(int(cursor.lastrowid))

    def rollback(self, campaign_id: int, actor: str) -> dict[str, Any]:
        """回滚不改写旧记录：以前一个已发布版本的参数生成新的升级活动（新策略版本）。"""
        campaign = self.repository.require_campaign(campaign_id)
        if campaign["status"] not in {"published", "applying", "applied"}:
            raise ConflictError("只有已发布的升级活动可以回滚")
        policy = self.repository.require_policy(int(campaign["policy_id"]))
        previous = record(self.connection.execute(
            "SELECT * FROM retest_policies WHERE crop_name=? AND risk_level=? AND version<? AND status='published' "
            "ORDER BY version DESC LIMIT 1",
            (campaign["crop_name"], campaign["risk_level"], policy["version"]),
        ).fetchone())
        if previous is None:
            raise ConflictError("没有可回滚到的历史策略版本")
        return self.create_campaign({
            "crop_name": previous["crop_name"],
            "risk_level": previous["risk_level"],
            "interval_months": previous["interval_months"],
            "warning_days": previous["warning_days"],
            "minimum_germination_percent": previous["minimum_germination_percent"],
            "effective_from": self.clock.now().date().isoformat(),
            "note": f"回滚 {campaign['campaign_no']}，恢复策略版本 {previous['version']}",
            "rollback_of": campaign["id"],
            "created_by": actor,
        })

    # ---------------------------------------------------------------- 预演

    def preview(self, campaign_id: int) -> dict[str, Any]:
        """计算受影响批次、新旧到期日和逾期变化，结果落库为确定快照供审批与发布使用。"""
        campaign = self.repository.require_campaign(campaign_id)
        if campaign["status"] not in {"draft", "previewed"}:
            raise ConflictError("只有草稿或已预演的升级活动可以预演")
        timestamp = to_storage(self.clock.now())
        today = self.clock.now().date()
        self.connection.execute("DELETE FROM retest_policy_campaign_items WHERE campaign_id=?", (campaign_id,))
        rows = self.connection.execute(
            "SELECT l.id AS lot_id,t.id AS test_id,t.germination_percent,t.completed_at "
            "FROM seed_lots l JOIN accessions a ON a.id=l.accession_id "
            "JOIN viability_tests t ON t.id=("
            "SELECT id FROM viability_tests WHERE lot_id=l.id AND status='completed' "
            "ORDER BY completed_at DESC,id DESC LIMIT 1) "
            "WHERE a.crop_name=? AND l.status NOT IN ('depleted','disposed')",
            (campaign["crop_name"],),
        ).fetchall()
        summary: dict[str, Any] = {
            "computed_at": timestamp,
            "crop_name": campaign["crop_name"],
            "risk_level": campaign["risk_level"],
            "candidate_interval_months": campaign["interval_months"],
            "affected_lots": 0,
            "to_recalculate": 0,
            "unchanged": 0,
            "conflicts": 0,
            "moved_earlier": 0,
            "moved_later": 0,
            "newly_overdue": 0,
            "cured_overdue": 0,
            "still_overdue": 0,
        }
        for row in rows:
            germination = float(row["germination_percent"])
            if risk_for_germination(germination) != campaign["risk_level"]:
                continue
            item = self._preview_item(campaign, row, germination, today, timestamp)
            summary["affected_lots"] += 1
            if item["conflict"]:
                summary["conflicts"] += 1
            elif item["change_type"] == "unchanged":
                summary["unchanged"] += 1
            else:
                summary["to_recalculate"] += 1
                if item["change_type"] == "earlier":
                    summary["moved_earlier"] += 1
                else:
                    summary["moved_later"] += 1
                if item["overdue_change"] == "newly_overdue":
                    summary["newly_overdue"] += 1
                elif item["overdue_change"] == "cured_overdue":
                    summary["cured_overdue"] += 1
                elif item["overdue_change"] == "still_overdue":
                    summary["still_overdue"] += 1
        current = self.repository.applicable_policy(campaign["crop_name"], campaign["risk_level"], today.isoformat())
        summary["current_policy"] = (
            {"id": current["id"], "version": current["version"], "interval_months": current["interval_months"]}
            if current else None
        )
        self.connection.execute(
            "UPDATE retest_policy_campaigns SET status='previewed',summary_json=?,updated_at=? WHERE id=?",
            (json.dumps(summary, ensure_ascii=False, sort_keys=True), timestamp, campaign_id),
        )
        return self.campaign_detail(campaign_id)

    def _preview_item(
        self,
        campaign: dict[str, Any],
        row: sqlite3.Row,
        germination: float,
        today: date,
        timestamp: str,
    ) -> dict[str, Any]:
        schedule = record(self.connection.execute(
            "SELECT * FROM retest_schedules WHERE lot_id=? AND status IN ('pending','notified','scheduled','waived') "
            "ORDER BY CASE status WHEN 'pending' THEN 0 WHEN 'notified' THEN 1 WHEN 'scheduled' THEN 2 ELSE 3 END,due_on "
            "LIMIT 1",
            (row["lot_id"],),
        ).fetchone())
        change_type = "conflict"
        conflict: str | None = None
        new_due: str | None = None
        overdue_change: str | None = None
        if schedule is None:
            conflict = "no_active_schedule"
        elif schedule["status"] == "scheduled":
            conflict = "already_scheduled"
        elif schedule["status"] == "waived":
            conflict = "manually_waived"
        else:
            old_due = date.fromisoformat(schedule["due_on"])
            completed_date = from_storage(row["completed_at"]).date()
            new_due_date = add_months(completed_date, int(campaign["interval_months"]))
            new_due = new_due_date.isoformat()
            if new_due_date < old_due:
                change_type = "earlier"
            elif new_due_date > old_due:
                change_type = "later"
            else:
                change_type = "unchanged"
            old_overdue = old_due < today
            new_overdue = new_due_date < today
            if old_overdue and new_overdue:
                overdue_change = "still_overdue"
            elif new_overdue:
                overdue_change = "newly_overdue"
            elif old_overdue:
                overdue_change = "cured_overdue"
            else:
                overdue_change = "not_overdue"
        cursor = self.connection.execute(
            "INSERT INTO retest_policy_campaign_items(campaign_id,lot_id,source_test_id,germination_percent,"
            "old_schedule_id,old_policy_id,old_due_on,new_due_on,change_type,overdue_change,conflict,status,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,'pending',?)",
            (
                campaign["id"], row["lot_id"], row["test_id"], germination,
                schedule["id"] if schedule else None,
                schedule["policy_id"] if schedule else None,
                schedule["due_on"] if schedule else None,
                new_due, change_type, overdue_change, conflict, timestamp,
            ),
        )
        return {"id": int(cursor.lastrowid), "change_type": change_type, "conflict": conflict, "overdue_change": overdue_change}

    # ---------------------------------------------------------------- 审批与发布

    def approve(self, campaign_id: int, actor: str) -> dict[str, Any]:
        campaign = self.repository.require_campaign(campaign_id)
        if campaign["status"] == "approved":
            return self.campaign_detail(campaign_id)
        if campaign["status"] != "previewed":
            raise ConflictError("只有已预演的升级活动可以审批")
        timestamp = to_storage(self.clock.now())
        self.connection.execute(
            "UPDATE retest_policy_campaigns SET status='approved',approved_by=?,approved_at=?,updated_at=? WHERE id=?",
            (actor, timestamp, timestamp, campaign_id),
        )
        return self.campaign_detail(campaign_id)

    def publish(self, campaign_id: int, actor: str) -> dict[str, Any]:
        """发布候选策略版本；此后完成的检测按新版本计算复检，库内存量日程由 apply 重算。"""
        campaign = self.repository.require_campaign(campaign_id)
        if campaign["status"] in {"published", "applying", "applied"}:
            return self.campaign_detail(campaign_id)
        if campaign["status"] != "approved":
            raise ConflictError("只有已审批的升级活动可以发布")
        timestamp = to_storage(self.clock.now())
        self.connection.execute(
            "UPDATE retest_policies SET status='published',published_at=? WHERE id=? AND status='candidate'",
            (timestamp, campaign["policy_id"]),
        )
        self.connection.execute(
            "UPDATE retest_policy_campaigns SET status='published',published_by=?,published_at=?,updated_at=? WHERE id=?",
            (actor, timestamp, timestamp, campaign_id),
        )
        return self.campaign_detail(campaign_id)

    # ---------------------------------------------------------------- 重算执行（可断点恢复、幂等）

    def apply(self, campaign_id: int) -> dict[str, Any]:
        """逐批次重算并入队通知。每条明细独立事务提交，中断后再次调用从待处理处继续；
        已完成的明细不会重复生成日程或重复入队提醒。上次失败的明细在下次调用时重试，
        单次调用内每条明细只尝试一次，确定性失败不会阻塞整批。"""
        campaign = self.repository.require_campaign(campaign_id)
        if campaign["status"] == "applied":
            return self.campaign_detail(campaign_id)
        if campaign["status"] not in {"published", "applying"}:
            raise ConflictError("升级活动尚未发布，不能执行重算")
        timestamp = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE retest_policy_campaigns SET status='applying',updated_at=? WHERE id=? AND status='published'",
                (timestamp, campaign_id),
            )
            connection.execute(
                "UPDATE retest_policy_campaign_items SET status='pending',error=NULL "
                "WHERE campaign_id=? AND status='failed'",
                (campaign_id,),
            )
        while True:
            item = record(self.connection.execute(
                "SELECT * FROM retest_policy_campaign_items WHERE campaign_id=? AND status='pending' "
                "ORDER BY id LIMIT 1",
                (campaign_id,),
            ).fetchone())
            if item is None:
                break
            try:
                with transaction(immediate=True):
                    self._apply_item(campaign, item)
            except Exception as exc:  # 单条失败不阻塞整批，记录后可在下次调用重试
                with transaction(immediate=True) as connection:
                    connection.execute(
                        "UPDATE retest_policy_campaign_items SET status='failed',error=?,processed_at=? WHERE id=?",
                        (str(exc)[:500], to_storage(self.clock.now()), item["id"]),
                    )
        remaining = int(self.connection.execute(
            "SELECT COUNT(*) FROM retest_policy_campaign_items WHERE campaign_id=? AND status IN ('pending','failed')",
            (campaign_id,),
        ).fetchone()[0])
        finished_at = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            if remaining == 0:
                connection.execute(
                    "UPDATE retest_policy_campaigns SET status='applied',applied_at=?,updated_at=? WHERE id=?",
                    (finished_at, finished_at, campaign_id),
                )
            else:
                connection.execute(
                    "UPDATE retest_policy_campaigns SET status='applying',updated_at=? WHERE id=?",
                    (finished_at, campaign_id),
                )
        return self.campaign_detail(campaign_id)

    def _apply_item(self, campaign: dict[str, Any], item: dict[str, Any]) -> None:
        timestamp = to_storage(self.clock.now())
        if item["conflict"] or item["change_type"] in {"unchanged", "conflict"}:
            self._finish_item(item["id"], "skipped", None, item["conflict"], None, timestamp)
            return
        # 预演之后可能发生人工干预，按当前状态再核对一次
        current = record(self.connection.execute(
            "SELECT * FROM retest_schedules WHERE lot_id=? AND status IN ('pending','notified','scheduled','waived') "
            "ORDER BY CASE status WHEN 'pending' THEN 0 WHEN 'notified' THEN 1 WHEN 'scheduled' THEN 2 ELSE 3 END,due_on "
            "LIMIT 1",
            (item["lot_id"],),
        ).fetchone())
        if current is None:
            self._finish_item(item["id"], "skipped", None, "no_active_schedule", None, timestamp)
            return
        if current["status"] == "scheduled":
            self._finish_item(item["id"], "skipped", None, "already_scheduled", None, timestamp)
            return
        if current["status"] == "waived":
            self._finish_item(item["id"], "skipped", None, "manually_waived", None, timestamp)
            return
        schedule_id = self._recalculate_schedule(campaign, item, timestamp)
        self._enqueue_notification(campaign, item, schedule_id, timestamp)
        self._finish_item(item["id"], "done", schedule_id, None, None, timestamp)

    def _recalculate_schedule(self, campaign: dict[str, Any], item: dict[str, Any], timestamp: str) -> int:
        existing = self.connection.execute(
            "SELECT id FROM retest_schedules WHERE lot_id=? AND policy_id=? AND due_on=? AND status IN ('pending','notified')",
            (item["lot_id"], campaign["policy_id"], item["new_due_on"]),
        ).fetchone()
        if existing:
            return int(existing[0])
        self.connection.execute(
            "UPDATE retest_schedules SET status='superseded',updated_at=? WHERE lot_id=? AND status IN ('pending','notified')",
            (timestamp, item["lot_id"]),
        )
        reason = f"策略升级 {campaign['campaign_no']} 按策略版本重算复检到期日"
        try:
            cursor = self.connection.execute(
                "INSERT INTO retest_schedules(lot_id,source_test_id,policy_id,due_on,status,reason,created_at,updated_at) "
                "VALUES(?,?,?,?,'pending',?,?,?)",
                (
                    item["lot_id"], item["source_test_id"], campaign["policy_id"], item["new_due_on"],
                    reason, timestamp, timestamp,
                ),
            )
            return int(cursor.lastrowid)
        except sqlite3.IntegrityError as exc:
            row = self.connection.execute(
                "SELECT id,status FROM retest_schedules WHERE lot_id=? AND due_on=?",
                (item["lot_id"], item["new_due_on"]),
            ).fetchone()
            if row and row["status"] in OPEN_SCHEDULE_STATUSES:
                return int(row["id"])
            raise ConflictError(
                "新到期日与该批次既有日程记录冲突",
                context={"lot_id": item["lot_id"], "due_on": item["new_due_on"]},
            ) from exc

    def _enqueue_notification(self, campaign: dict[str, Any], item: dict[str, Any], schedule_id: int, timestamp: str) -> None:
        payload = {
            "campaign_id": campaign["id"],
            "campaign_no": campaign["campaign_no"],
            "lot_id": item["lot_id"],
            "schedule_id": schedule_id,
            "old_due_on": item["old_due_on"],
            "new_due_on": item["new_due_on"],
            "policy_id": campaign["policy_id"],
        }
        self.connection.execute(
            "INSERT OR IGNORE INTO outbox_events(event_key,event_type,aggregate_type,aggregate_id,payload_json,"
            "available_at,created_at) VALUES(?,?,?,?,?,?,?)",
            (
                f"retest-recalc:{campaign['id']}:{item['lot_id']}",
                "retest_schedule_recalculated", "seed_lot", str(item["lot_id"]),
                json.dumps(payload, ensure_ascii=False, sort_keys=True), timestamp, timestamp,
            ),
        )

    def _finish_item(
        self,
        item_id: int,
        status: str,
        schedule_id: int | None,
        conflict: str | None,
        error: str | None,
        timestamp: str,
    ) -> None:
        self.connection.execute(
            "UPDATE retest_policy_campaign_items SET status=?,new_schedule_id=COALESCE(?,new_schedule_id),"
            "conflict=COALESCE(?,conflict),error=?,processed_at=? WHERE id=?",
            (status, schedule_id, conflict, error, timestamp, item_id),
        )

    # ---------------------------------------------------------------- 豁免

    def waive_schedule(self, schedule_id: int, actor: str, reason: str) -> dict[str, Any]:
        row = record(self.connection.execute(
            "SELECT * FROM retest_schedules WHERE id=?", (schedule_id,)
        ).fetchone())
        if row is None:
            raise NotFoundError("复检日程不存在")
        if row["status"] not in OPEN_SCHEDULE_STATUSES:
            raise ConflictError("只有待执行的复检日程可以人工豁免")
        timestamp = to_storage(self.clock.now())
        cursor = self.connection.execute(
            "UPDATE retest_schedules SET status='waived',waived_by=?,waived_at=?,waived_reason=?,updated_at=? "
            "WHERE id=? AND status IN ('pending','notified')",
            (actor, timestamp, reason, timestamp, schedule_id),
        )
        if cursor.rowcount != 1:
            raise ConflictError("复检日程状态已变化，不能豁免")
        return record(self.connection.execute(
            "SELECT * FROM retest_schedules WHERE id=?", (schedule_id,)
        ).fetchone()) or {}

    # ---------------------------------------------------------------- 查询

    def campaign_detail(self, campaign_id: int) -> dict[str, Any]:
        campaign = self.repository.require_campaign(campaign_id)
        items = records(self.connection.execute(
            "SELECT i.*,l.lot_no FROM retest_policy_campaign_items i JOIN seed_lots l ON l.id=i.lot_id "
            "WHERE i.campaign_id=? ORDER BY i.id",
            (campaign_id,),
        ).fetchall())
        progress = {"total": len(items), "pending": 0, "done": 0, "skipped": 0, "failed": 0}
        conflicts: list[dict[str, Any]] = []
        for item in items:
            progress[item["status"]] += 1
            if item["conflict"]:
                item["conflict_label"] = _CONFLICT_LABELS.get(item["conflict"], item["conflict"])
                conflicts.append(item)
        campaign["summary"] = json.loads(campaign.pop("summary_json") or "{}")
        campaign["conflicts"] = conflicts
        campaign["progress"] = progress
        return campaign

    def list_campaigns(self, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            rows = self.connection.execute(
                "SELECT * FROM retest_policy_campaigns WHERE status=? ORDER BY id DESC", (status,)
            ).fetchall()
        else:
            rows = self.connection.execute("SELECT * FROM retest_policy_campaigns ORDER BY id DESC").fetchall()
        campaigns = records(rows)
        for campaign in campaigns:
            campaign["summary"] = json.loads(campaign.pop("summary_json") or "{}")
        return campaigns

    def list_items(self, campaign_id: int) -> list[dict[str, Any]]:
        self.repository.require_campaign(campaign_id)
        items = records(self.connection.execute(
            "SELECT i.*,l.lot_no FROM retest_policy_campaign_items i JOIN seed_lots l ON l.id=i.lot_id "
            "WHERE i.campaign_id=? ORDER BY i.id",
            (campaign_id,),
        ).fetchall())
        for item in items:
            if item["conflict"]:
                item["conflict_label"] = _CONFLICT_LABELS.get(item["conflict"], item["conflict"])
        return items

    def schedules(self, campaign_id: int) -> list[dict[str, Any]]:
        """发布后由本次升级确定的到期列表：只含重算生成的有效日程，顺序固定。"""
        self.repository.require_campaign(campaign_id)
        return records(self.connection.execute(
            "SELECT s.*,l.lot_no,a.accession_no,a.crop_name FROM retest_policy_campaign_items i "
            "JOIN retest_schedules s ON s.id=i.new_schedule_id "
            "JOIN seed_lots l ON l.id=i.lot_id JOIN accessions a ON a.id=l.accession_id "
            "WHERE i.campaign_id=? AND i.new_schedule_id IS NOT NULL ORDER BY s.due_on,l.lot_no",
            (campaign_id,),
        ).fetchall())

    def explain_lot(self, lot_id: int, as_of: datetime | None = None) -> dict[str, Any]:
        """按某个时点解释批次的复检依据：作物、风险等级、最近有效检测和当时采用的策略版本。"""
        lot = self.repository.require_lot(lot_id)
        accession = self.repository.require_accession(int(lot["accession_id"]))
        moment = as_of or self.clock.now()
        as_of_storage = to_storage(moment)
        test = record(self.connection.execute(
            "SELECT * FROM viability_tests WHERE lot_id=? AND status='completed' AND completed_at<=? "
            "ORDER BY completed_at DESC,id DESC LIMIT 1",
            (lot_id, as_of_storage),
        ).fetchone())
        schedule = record(self.connection.execute(
            "SELECT * FROM retest_schedules WHERE lot_id=? AND status IN ('pending','notified','scheduled') "
            "ORDER BY due_on LIMIT 1",
            (lot_id,),
        ).fetchone())
        explanation: dict[str, Any] = {
            "lot_id": lot["id"],
            "lot_no": lot["lot_no"],
            "crop_name": accession["crop_name"],
            "as_of": as_of_storage,
            "latest_test": None,
            "risk_level": None,
            "policy": None,
            "due_on": None,
            "current_schedule": schedule,
        }
        if test is None:
            explanation["basis"] = "该时点之前没有已完成的活力检测，无法推导复检依据"
            return explanation
        germination = float(test["germination_percent"])
        risk = risk_for_germination(germination)
        policy = self.repository.policy_as_of(accession["crop_name"], risk, as_of_storage)
        completed_date = from_storage(test["completed_at"]).date()
        explanation["latest_test"] = {
            "id": test["id"],
            "test_no": test["test_no"],
            "germination_percent": germination,
            "completed_at": test["completed_at"],
        }
        explanation["risk_level"] = risk
        if policy is None:
            explanation["basis"] = "该时点没有已发布的适用复检策略"
            return explanation
        explanation["policy"] = {
            "id": policy["id"],
            "version": policy["version"],
            "interval_months": policy["interval_months"],
            "warning_days": policy["warning_days"],
            "effective_from": policy["effective_from"],
            "published_at": policy["published_at"],
        }
        explanation["due_on"] = add_months(completed_date, int(policy["interval_months"])).isoformat()
        explanation["basis"] = (
            f"按 {as_of_storage[:10]} 时点：作物 {accession['crop_name']}，最近有效检测 {test['test_no']} "
            f"发芽率 {germination:.2f}% 对应 {risk} 风险，采用策略版本 {policy['version']}"
            f"（间隔 {policy['interval_months']} 个月）"
        )
        return explanation
