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
- `app/germplasm/policy_campaigns.py` 管理复检策略升级：候选策略、预演、审批发布、断点可恢复的日程重算与回滚。
- `app/germplasm/quality.py` 管理温湿度读数、偏离告警和种质发放审批。
- `app/api`、`app/services` 和 `app/repositories` 提供身份、权限、审计、后台作业及维护能力。

## 复检策略升级流程

策略变更不再直接生效，而是走版本化的升级活动（`/api/germplasm/policy-campaigns`）：

1. **创建候选**：登记新间隔等参数，生成 `candidate` 状态的策略版本，不影响现有日程与检测。
2. **预演**：`POST /{id}/preview` 计算受影响批次、新旧到期日和逾期变化，快照落库；已安排检测和已人工豁免的批次列为冲突，不参与重算。
3. **审批发布**：`POST /{id}/approve` 后 `POST /{id}/publish`，候选策略转为 `published`，此后完成的检测按新版本计算复检。
4. **重算执行**：`POST /{id}/apply` 逐批次重算尚未执行且未豁免的日程并入队通知；每条明细独立事务提交，中断后重复调用从断点继续，同一版本重复执行不会产生重复日程或重复提醒。已完成检测的历史记录与旧日程行保持不变。
5. **回滚**：`POST /{id}/rollback` 以前一已发布版本的参数生成新的升级活动（新策略版本），不改写旧记录，同样经预演、审批、发布后生效。

负责人可通过 `GET /policy-campaigns/{id}` 查看预演摘要与冲突批次，通过 `GET /policy-campaigns/{id}/schedules` 查看发布后确定的到期列表；`GET /lots/{lot_id}/retest-explanation?as_of=...` 按指定时点解释当时采用的作物、风险等级、最近有效检测和策略版本。

## 一致性约定

SQLite 连接启用外键、WAL、忙等待和即时写事务。资源档案、库位、容器摆放、检测任务和发放申请采用版本号防止旧请求覆盖新状态；入库、移库、取样和传感读数使用业务键去重。活力检测保留采用的规程版本和每个重复的观察计数，完成后可依据作物及风险策略生成下一次复检日期。复检策略按版本发布，候选版本不参与检测计算；同一批次的有效日程由部分唯一索引保护，升级重算与通知入队按明细幂等推进。会话令牌只保存摘要，审计记录不保存明文密码或令牌。
