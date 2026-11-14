# 园区电表区间用电量与日用量质量 API

把网关补传的电表**累计读数**转换成**区间用电量**，并按电表号 + UTC 日期
签发供日报入仓的**日用量质量结果**。

技术栈：FastAPI + SQLite（WAL），Docker Compose 启动两个 API 进程共享同一个
数据库卷。

## 快速开始

```bash
docker compose up -d --build
```

| 地址 | 用途 |
| --- | --- |
| <http://localhost:8000/docs> | Swagger UI / api-1（可执行入仓、撤销、查询） |
| <http://localhost:8000/redoc> | ReDoc |
| <http://localhost:8000/openapi.json> | OpenAPI 规范 |
| <http://localhost:8002/docs> | api-2，第二个进程，同一份数据 |
| <http://localhost:8000/health> | 健康检查（含当前提交序号） |

两个端口背后是**两个独立的 API 进程，共用 `/data/energy.sqlite3`**；可以交替
向两个端口发送写入以验证跨进程一致性（`scripts/e2e_compose.py` 就是这样做的）。

数据保存在命名卷 `energy-data` 中：`docker compose down` 保留数据，
`docker compose down -v` 清空重来。想加载演示数据（含乱序补传、换表、
计数回退样例，以及一只整点齐全、可直接签发日用量的电表 M-1003）时设置
`ENERGY_SEED_DEMO=1` 后启动 api 容器，例如：

```bash
ENERGY_SEED_DEMO=1 docker compose up -d --build
curl -s localhost:8000/api/v1/meters | python3 -m json.tool
curl -s -X POST localhost:8000/api/v1/daily-results/issue \
  -H 'content-type: application/json' \
  -d '{"meter_id":"M-1003","date":"2026-10-01"}' | python3 -m json.tool
```

本地（非 Docker）运行：

```bash
python3 -m pip install -r requirements-dev.txt
ENERGY_DB_PATH=/tmp/energy.sqlite3 python3 -m app.run     # http://127.0.0.1:8000/docs
python3 -m pytest                                        # 含双进程并发/崩溃测试
```

## API

### 入仓 — `POST /api/v1/readings`

一次提交一批读数，**整批原子提交**（要么全部生效，要么整批拒绝）。

```json
{
  "readings": [
    {
      "meter_id": "M-1001",
      "source_record_id": "gw-0001",
      "read_at": "2026-10-01T00:00:00Z",
      "reading": 1000,
      "meter_replaced": false
    }
  ]
}
```

| 字段 | 说明 |
| --- | --- |
| `meter_id` | 电表号 |
| `source_record_id` | 源记录号，全局唯一，用于重试去重与撤销 |
| `read_at` | UTC 时刻（ISO-8601）。带时区会换算为 UTC；不带时区按 UTC 解释 |
| `reading` | 累计读数，非负整数（度）；接受整数、整值浮点、数字字符串 |
| `meter_replaced` | 换表标记，缺省 `false`；接受 `true/false` 或 `1/0` |

返回：

```json
{
  "commit_seq": 3,
  "changed": true,
  "counts": {"created": 2},
  "records": [{"source_record_id": "gw-0001", "status": "created"}, ...],
  "affected_meters": [{"meter_id": "M-1001", "reading_count": 2,
                       "interval_count": 1, "break_count": 0,
                       "usage_total": 120, "first_read_at": "...", "last_read_at": "..."}]
}
```

`records[].status` 为 `created`（新写入）、`unchanged`（同号同内容重试，
不重复写、不推进提交序号）。整份重试时 `changed=false`、`counts` 全为
`unchanged`。

### 撤销 — `POST /api/v1/readings/undo`

```json
{"source_record_ids": ["gw-0001", "gw-0002"]}
```

按源记录号删除读数并重算这些电表的**全部区间**；不存在的号码返回
`not_found`（不算失败）；撤销后相邻读数会自动连成新的区间。

### 查询 — `GET /api/v1/meters/{meter_id}`

可选参数 `from` / `to`（ISO-8601，含端点）与 `include_intervals`。
响应来自**同一个数据库快照**：

```json
{
  "meter_id": "M-1001",
  "commit_seq": 5,
  "window": {"from": null, "to": null, "applied": false},
  "readings": [{"source_record_id": "...", "read_at": "...", "reading": 1000, ...}],
  "intervals": [{
      "from_source_record_id": "...", "to_source_record_id": "...",
      "from_read_at": "...", "to_read_at": "...",
      "from_reading": 1000, "to_reading": 1120,
      "usage": 120, "is_break": false, "break_reason": null, "commit_seq": 5 }],
  "breaks": [...],
  "usage_total": 120
}
```

`GET /api/v1/meters` 列出所有电表的汇总。无任何读数的电表返回 404。

### 签发日用量质量结果 — `POST /api/v1/daily-results/issue`

园区能耗工程师核准单表日用量后交给日报入仓。按电表号与 UTC 日期签发：

```json
{"meter_id": "M-1003", "date": "2026-10-01"}
```

核对窗口是**当日 00:00 到次日 00:00（两端都算）**，共 25 个逐小时整点。

* **整点缺读数，或窗口内有换表 / 计数回退断点**：返回
  `status="not_issued"`，列出每个缺失时刻（`missing_slots[].hour`，
  0–23 为当日整点、24 为次日 00:00 端点）与每个断点（`breaks[]`），
  **不写入任何结果**。
* **整点齐全且无断点**：汇总窗口内全部相邻区间的用电量（望远镜求和，
  结果等于次日 00:00 读数 − 当日 00:00 读数，非整点补传读数也会参与求和），
  保存为当日 `valid` 结果。

成功响应：

```json
{
  "meter_id": "M-1003",
  "date": "2026-10-01",
  "window": {"start": "2026-10-01T00:00:00Z", "end": "2026-10-02T00:00:00Z",
             "inclusive": true},
  "status": "valid",
  "usage_total": 288,
  "interval_count": 24,
  "missing_slots": [],
  "breaks": [],
  "commit_seq": 6,
  "issued_commit_seq": 6,
  "idempotent_retry": false
}
```

签发结果的生命周期：

* **读数变更即过期**：该表在窗口内实际新增或撤销读数时（入仓 / 撤销），
  已签发结果在**同一个提交事务**里被标为 `expired`，不可能留下与读数
  不符的有效结果。午夜 00:00 的读数同时属于前后两天，变更它会使两天的
  结果同时过期。
* **相同内容重试**：读数未变时重发签发请求是幂等的
  （`idempotent_retry=true`），不新增行、不推进提交序号、状态不变。
* **重签从最新已提交状态计算**：读数变化后旧结果已过期；重新签发成功
  会基于当前读数写入新的 `valid` 行，签发失败则保留明确的 `expired` 行，
  不会把它复活或覆盖。

### 查询日用量结果 — `GET /api/v1/daily-results/{meter_id}/{date}`

查询只能得到**有效日用量或明确过期**，二者必居其一；该表该日从未签发时
返回 404 `daily_result_not_found`：

```json
{
  "meter_id": "M-1003",
  "date": "2026-10-01",
  "window": {"start": "2026-10-01T00:00:00Z", "end": "2026-10-02T00:00:00Z",
             "inclusive": true},
  "status": "expired",
  "usage_total": 288,
  "interval_count": 24,
  "reading_count": 25,
  "missing_slot_count": 0,
  "break_count": 0,
  "issued_commit_seq": 6,
  "expired_commit_seq": 9,
  "commit_seq": 9
}
```

过期行保留签发当时的用量等历史信息，并记录使其过期的提交序号
`expired_commit_seq`。`GET /api/v1/daily-results` 列出每个电表每日最新
一条结果，可用 `meter_id` / `date` 查询参数过滤。

### 错误

| 状态码 | 含义 |
| --- | --- |
| 409 `batch_rejected` | 整批拒绝，`conflicts[]` 说明每条冲突 |
| 422 | 请求体/时间窗口参数/日期格式不合法 |
| 404 `meter_not_found` | 该电表没有读数 |
| 404 `daily_result_not_found` | 该电表该日从未签发日用量结果 |
| 503 `database_busy` | 等待写锁超时，可重试（带 `Retry-After`） |

## 规则与语义

* **区间 = 同表相邻两条读数之差。** 读数按 `(电表号, UTC 时刻)` 排序，
  无论到达顺序如何（乱序补传会把新读数插到中间并重算前后区间）。
* **断点**：以下两种情况记断点，该区间 `usage` 为 `null`，不推算用量：
  * 后一条读数带 `meter_replaced=true`（换表）；
  * 后一条读数小于前一条（计数回退）。
  断点区间的两端读数仍会被后续正常区间使用。
* **同号同内容重试**幂等：不写库、不推进提交序号、不重算。
* **同号异内容**、**同表同刻（不同源记录号）**：整批 409 拒绝，数据库保持
  原样（校验在写入之前完成）。
* 同一次请求里重复的源记录号：内容相同则合并；内容不同或同表同刻不同号则
  整批拒绝。
* 撤销后再提交同一源记录号视为全新记录。

## 一致性、并发与故障安全

* 共享库、两个进程：所有写操作在 `BEGIN IMMEDIATE` 事务里执行，SQLite 的
  写锁把并发写串行化（`busy_timeout=30s`），不会出现 SQLite 层面互相覆盖。
  `(source_record_id)` 与 `(meter_id, read_at_us)` 的唯一约束在库内兜底。
* 读数与区间在**同一个事务**内写入；每次变更后该电表的区间全部重建，
  因此区间表始终是读数表的函数。
* 日用量结果的过期与读数变更在**同一个事务**内提交：入仓或撤销一旦提交，
  受影响日（含午夜读数关联的前后两天）的有效结果必然同时变为过期；事务
  回滚则读数与过期动作一起撤销。数据库内的部分唯一索引
  `idx_daily_one_valid` 保证每个 `(电表号, UTC 日)` 至多一条 `valid` 行，
  `CHECK` 约束保证 `valid` 行必有用量、无缺失整点、无断点。
* 日用量查询在显式读事务（WAL 快照）中执行，只返回每个 `(电表号, 日)`
  最新一条结果——要么 `valid` 要么明确 `expired`，不会出现“有效但与当前
  读数不符”的状态。
* 两个 API 进程并发签发与改动时，写锁串行化所有签发、入仓、撤销；相同
  内容重试不改变状态，重签从最新已提交状态计算。
* 失败/重启：整笔提交在一个事务内，未提交即回滚，进程被杀不会留下半笔
  结果，也不会留下与读数不符的有效日用量；启动时校验 `user_version`，
  v1 库会原地升级到当前 schema（保留全部既有读数、区间与提交序号），
  测试另跑 `PRAGMA integrity_check` 与 `foreign_key_check`。
* 区间表有 `CHECK` 约束：断点区间 `usage` 必须为 `NULL`，正常区间必须有值。

## 测试

```bash
python3 -m pytest            # 69 个用例
```

* `tests/test_api.py`：区间/断点/乱序/幂等/冲突/撤销/窗口/校验。
* `tests/test_daily.py`：日用量签发的 25 整点核对、缺失时刻与断点上报、
  成功汇总、幂等重试、读数变更同事务过期、午夜跨天、查询与校验。
* `tests/test_concurrency.py`：启动两个真实进程共享同一个 SQLite 文件，
  并发写、并发重试、竞争冲突、持续读写一致性、进程崩溃（`os._exit`）、
  重启后数据延续、未提交事务回滚。
* `tests/test_daily_concurrency.py`：双进程并发签发只产生一条有效行、
  库内部分唯一索引兜底、签发与读数改动并发期间有效结果始终与读数一致、
  进程崩溃不留矛盾有效行、重启后过期状态延续并可重签。
* `tests/test_durability.py`：区间重建中途异常时整批回滚、提交序号不前进、
  多进程同时初始化建库、schema 版本保护。
* `tests/test_daily_durability.py`：v1→v2 schema 原地升级并保留数据、
  签发失败整笔回滚、过期随区间重建失败一起回滚、`daily_results` 的
  `CHECK` 约束。

验证 Compose 部署：

```bash
python3 scripts/e2e_compose.py http://localhost:8000 http://localhost:8002
```

## 目录结构

```
app/            FastAPI 应用（main.py 路由、service.py 读数/区间领域逻辑、
                daily.py 日用量签发与过期、db.py 存储）
tests/          功能与并发/故障测试
scripts/        Compose 部署冒烟脚本
Dockerfile, docker-compose.yml
```
