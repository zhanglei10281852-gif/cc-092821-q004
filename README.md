# 种质资源入库与活力复检服务

本项目是面向种质资源库的 Python 后端服务，用于登记采集或引进材料、建立种子批次、管理低温库位和容器移动、执行发芽活力检测、生成复检日程并处理环境与质量告警。档案、库存、检测和发放审批都保存在本地 SQLite 中，关键写入带版本或幂等键，适合在单个 Linux 应用容器内运行。

## 运行环境

- Python 3.11
- FastAPI 与 Uvicorn
- SQLite 3，由 Python 标准库提供

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

默认数据库位于 `data/germplasm.db`，也可以通过 `GERMPLASM_DATABASE_PATH` 指向其他 `.db`、`.sqlite` 或 `.sqlite3` 文件。

## 初始化与启动

```bash
python -m app.cli init-db
python -m app.cli check-db
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

健康检查为 `GET /api/system/health`。首次使用可调用 `POST /api/auth/bootstrap` 创建管理员，再通过 `POST /api/auth/login` 取得 Bearer 会话令牌。种质业务接口统一位于 `/api/germplasm`。

## 测试与构建检查

```bash
python -m pytest
python -m compileall -q app tests
```

下面两条命令分别检查 HTTP 入口和完整的入库演示链路：

```bash
python -m app.cli smoke
python -m app.cli demo
```

## 业务边界

- `app/germplasm/accessions.py` 管理来源、资源档案、护照信息与接收状态。
- `app/germplasm/inventory.py` 管理批次、库位容量、容器摆放、移动、领用和冻结。
- `app/germplasm/viability.py` 管理检测规程、取样、重复计数、活力结果与复检日程。
- `app/germplasm/policy_revisions.py` 管理复检策略版本的预演、审批、发布、回滚与时点解释。
- `app/germplasm/quality.py` 管理温湿度读数、偏离告警和种质发放审批。
- `app/api`、`app/services` 和 `app/repositories` 提供身份、权限、审计、后台作业及维护能力。

## 复检策略版本治理

策略升级走"候选 → 预演 → 审批 → 发布"流程，全部位于 `/api/germplasm`：

1. `POST /policy-revisions` 创建候选版本（draft），不影响任何现有日程。
2. `POST /policy-revisions/{id}/preview` 预演：计算受影响批次、新旧到期日、逾期变化与需提醒数量；`GET .../preview` 查看摘要与明细，`GET .../conflicts` 查看冲突批次（人工豁免、已安排检测的批次会保留现状）。
3. `POST /policy-revisions/{id}/approve` 审批（必须先预演）。
4. `POST /policy-revisions/{id}/publish` 发布：只重算尚未执行且未人工豁免的日程，已完成检测的历史依据保持不变。发布按批次独立提交并记录游标，中断后重复调用即从断点恢复（可用 `batch_size` 分批）；同一版本重复发布是幂等的，不会制造第二条有效日程或重复提醒（提醒经 outbox 以确定键入队）。`GET .../due-list` 返回发布后的确定性到期列表。
5. `POST /policy-revisions/rollback` 回滚：复制目标历史版本参数生成新的候选版本，旧记录不被改写，随后同样走预演、审批、发布。
6. `GET /lots/{lot_id}/retest-explanation?as_of=...` 按时点解释当时采用的作物、风险等级、最近有效检测、策略版本与应复检日期。

临近到期清单 `GET /retest-schedules/due` 会带出每条日程的策略版本与间隔，便于区分日期来源。`POST /retest-schedules/{id}/waive` 可将待执行日程人工豁免，豁免日程在发布重算时保留。`POST /policies` 直接建版立即生效的旧接口仍然可用，但策略升级建议使用上述治理流程。

## 一致性约定

SQLite 连接启用外键、WAL、忙等待和即时写事务。资源档案、库位、容器摆放、检测任务和发放申请采用版本号防止旧请求覆盖新状态；入库、移库、取样和传感读数使用业务键去重。活力检测保留采用的规程版本和每个重复的观察计数，完成后可依据作物及风险策略生成下一次复检日期。复检策略按版本留存完整生命周期（候选、审批、发布、取代），每个批次至多存在一条有效复检日程（部分唯一索引保证），发布运行按批次记录游标与结果行，提醒事件以确定键写入 outbox。会话令牌只保存摘要，审计记录不保存明文密码或令牌。
