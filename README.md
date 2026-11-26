# 备份校验退机交接演练（FastAPI + SQLite）

备份还没验完，值班员要停掉一台校验机。本项目是一个**本地可跑的演练系统**：

- 两台**独立 OS 进程**校验机 `m1` / `m2`，从同一份**只读样例卷**逐段**真实计算 SHA-256**，与清单核对；
- 每台**最多同时验 2 段**（服务端事务 + Worker 自约束双保险）；
- 在 **`/docs`** 提交作业、设置各段耗时（演练“拍”）、手工推进演练时钟；
- 选定要停的进程，填**最晚停机拍**和**最多可重验的在途段数**；
- 受理后旧机进入 `draining`，**停领新段**，另一台照常接单；
- 在途段可**等待验完**或**交回队列**由另一台重验；
- 只有 `Popen.poll()`/`waitpid` **确认 OS 进程实际退出**，`confirm-exit` 才返回
  “可检修”报告，给出 **停机拍 `stop_tick`** 与 **重验段号 `reverify_segment_numbers`**；
- 支持把旧机回报用延迟箱**留到停机后再放出**：迟到结果被围栏拒绝、**不得入账**；
- 每段只有一个正式结论：`UNIQUE(job_id, seg_number)` + 禁止改/删结论的触发器 + 应用层裁决；
- 限额与截止拍**无法兼顾**时返回 `409` 和 `blocking_segments`，**不动任何状态，原作业继续**；
- `GET /jobs/{id}/segments` 查每段的**实算摘要**（可能有多条，含旧机与重验机）和**唯一正式结论**；
- 支持 **`docker compose up --build`** 一键启动。

---

## 1. 快速开始

### 方式 A：Docker Compose（推荐）

```bash
docker compose up --build
# 打开 http://127.0.0.1:8000/docs
```

容器以 `read_only: true` 运行，根文件系统（含 `/srv/sample_volume`）不可写，
只有命名卷 `drill-data:/data`（SQLite）可写。镜像构建期已生成确定性样例卷并去掉写位。

试跑完整退机交接（41 项断言）：

```bash
# 与 API 在同一容器/PID 命名空间，OS 级 PID 旁证也会执行
docker compose exec drill python scripts/drill_handoff.py

# 或用独立的一次性 test profile 容器（跨容器时自动跳过 OS PID 旁证）
docker compose run --rm drill-test
```

### 方式 B：本机 Python（3.11+）

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

# 可选：预生成样例卷（不执行则启动时自动生成）
python -m app.volume --dir sample_volume

python -m app.main
# 或 PORT=8000 uvicorn app.main:app --host 127.0.0.1 --port 8000
```

另开终端：

```bash
python scripts/drill_handoff.py                 # 默认 http://127.0.0.1:8000
python scripts/selftest_ledger.py               # 账本级 + 篡改/mismatch 自检
```

---

## 2. 在 `/docs` 里的操作顺序

1. `POST /admin/reset`：复位（停进程 → 清库重建 → 重新拉起两台）；
2. `POST /jobs`：提交作业，例如
   ```json
   {"segment_count": 4,
    "segment_durations": {"1": 2, "2": 3, "3": 5, "4": 7}}
   ```
   也可之后 `PATCH /jobs/{job_id}/segments/{n}` 改某段耗时；
3. `GET /machines`：看两台 PID/代次，以及如何各领满 2 段、`due_tick` 是多少；
4. `POST /clock/advance`，body `{"to_tick": N}` 或 `{"ticks": k}`，推进演练时钟；
5. （可选，制造迟到回报）`POST /machines/m1/holds` `{"segment_number": 4}`；
6. `POST /shutdowns`：
   ```json
   {"machine_id": "m1", "deadline_tick": 10, "max_reverify": 1}
   ```
   - 可行 → `202 accepted`，给出 `wait_segment_numbers` / `reverify_segment_numbers`
     / `planned_stop_tick`；
   - 不可行 → `409` + `blocking_segments`，机器仍 `active`，作业继续；
7. 推进时钟到 `planned_stop_tick`，轮询
   `POST /shutdowns/{shutdown_id}/confirm-exit`，直到 `200`（进程真退出）；
8. 若第 5 步截获了回报：`GET /held-reports` 找 `held_id`，
   停机后 `POST /held-reports/{id}/release`，观察 `machine_exited` 拒绝；
9. 继续推进时钟，让 m2 完成重验；`GET /jobs/{job_id}/segments` 核对摘要与唯一结论；
10. `GET /rejected-reports` 看全部拒绝留痕；`POST /machines/m1/start` 可让旧机检修后重新上线。

---

## 3. 停机判定规则

设受理瞬间演练拍为 `T0`，机器上每个在途段的应完拍为 `due_tick = claimed_tick + 耗时快照`。

- `due_tick <= deadline_tick`：默认决定 `wait`（等旧机自己验完，不占重验额度）；
- `due_tick > deadline_tick`：默认决定 `return`（必须交回队列，由另一台重验，占 1 个额度）；
- 也可用 `decisions: {"3": "wait", "4": "return"}` 逐段覆盖。

不可受理（返回 `409 quota_cannot_meet_deadline` + `blocking_segments`）的情况：

- 需要 `return` 的段数 `> max_reverify`；或
- 显式对 `due_tick > deadline_tick` 的段选了 `wait`（`wait_misses_deadline`）；或
- `deadline_tick` 早于当前拍。

被阻塞时**只写一条 `blocked` 停机单留痕**，不修改机器状态与任何租约，所以“原作业继续”。

可受理时在同一个 `BEGIN IMMEDIATE` 事务里：机器置 `draining`、`return` 段清空租约回到
`pending`、写 `accepted` 停机单。`planned_stop_tick = max(当前拍, 所有 wait 段 due_tick)`，
必然 `<= deadline_tick`。

### 退出确认

Worker 发现自己 `draining` 且本地任务全部了结（等待段已回报、交回段已最佳努力投递）后
`exit 0`。后台 reaper 用 `Popen.poll()` 回收；`confirm-exit` 再次以 `poll()` 为硬凭据，
进程仍活着就 `409 process_not_exited`，真退出才回“可检修”报告。

---

## 4. 为什么迟到结果进不了账（三道围栏 + 一道唯一约束）

`apply_result()` 在一个立即写事务里按顺序校验：

1. **代次围栏 `generation_fenced`**：每次（重新）启动代次 +1，旧进程/旧消息失效；
2. **唯一结论 `conclusion_exists`**：该段已有正式结论就拒绝（数据库还有
   `UNIQUE(job_id, seg_number)` 并发兜底）；
3. **进程状态 `machine_exited`**：已确认退出的机器不能再写账本；
4. **租约围栏 `lease_superseded`**：token/属主/代次必须仍是当前有效租约
   （交回后被 m2 重领，旧 token 立即失效）；
5. **时序 `not_due`**：未到 `due_tick` 不允许提前记结论；
6. 通过后才比对清单，写 `verified` / `mismatch` 结论并把段置 `completed`。

所有被拒回报都进 `rejected_reports`（含原因与原报文）。`conclusions` 上还有
`BEFORE UPDATE/DELETE` 触发器 `RAISE(ABORT)`，保证正式结论不可改、不可删。

“实算摘要”与“正式结论”分开：`computations` 是 append-only 的遥测，同一段可以同时看到
m1 的旧实算和 m2 的重验实算（二者 SHA 相同正好互证）；`conclusions` 每段至多一行。

---

## 5. 架构与代码地图

```
docker-compose.yml      单服务 drill（API + 监管两个 worker 子进程）+ 可选 drill-test
Dockerfile              python:3.12-slim，构建期生成只读样例卷，非 root，HEALTHCHECK
app/
  main.py               FastAPI、lifespan（建库/生卷/拉起两进程/reaper/优雅停止）
  config.py             环境变量配置；MAX_INFLIGHT_PER_MACHINE=2
  db.py                 SQLite schema、触发器、immediate_tx 跨进程写事务
  volume.py             确定性样例卷 + manifest.json + O_RDONLY 实算 SHA-256
  schemas.py            Pydantic 模型与 /docs 示例
  services.py           全部核心裁决：领段/时钟/作业/结论/停机可行性/延迟箱
  supervisor.py         两个独立 OS 进程的 spawn、waitpid 回收、退出硬确认
  worker.py             独立进程入口：轮询、实算、到期回报、draining 后优雅退出
  routers/              system / clock / jobs / machines / shutdowns / held
scripts/
  drill_handoff.py      端到端退机交接试跑（41 项断言，退出码 0=全过）
  selftest_ledger.py    哈希/篡改、mismatch、唯一结论、触发器自检（8 项断言）
```

SQLite 默认 `data/drill.db`（compose 中为 `/data/drill.db` 命名卷），WAL +
`busy_timeout=8s`。Worker 只通过 HTTP/JSON 与 API 交互，所有写由 FastAPI 侧的
立即事务串行化，因此两台进程并发领段不会重段。

---

## 6. 实测记录（本仓库交付前真实执行）

环境：Linux x86_64，Python 3.11.2，依赖 `fastapi==0.165.0`、
`uvicorn[standard]==0.54.0`、`httpx==0.28.1`（装在 `.venv`）。
测试服务：`PORT=8011 ... python -m app.main`，Worker 轮询 0.15s。

- `python -m compileall app scripts`：通过；
- `python scripts/selftest_ledger.py`：**8/8 PASS**
  （含篡改 1 字节摘要不同、错误摘要→`mismatch`、二次回报 `conclusion_exists`、
  触发器禁止 UPDATE/DELETE）；
- `python scripts/drill_handoff.py`：前后运行三次（含连续第二次验证
  `/admin/reset` 可重复停机/拉起，以及监管器回收逻辑调整后的最终代码），
  均 **41/41 PASS，退出码 0**；另测了显式 `decisions` 的 `wait_misses_deadline`
  阻塞与两段全 `return` 时 `stop_tick=0` 立即退出；
- `/docs`、`/redoc`、`/openapi.json` 均 HTTP 200，24 个路径全部上架；
- `ps`/`/health` 可见两个不同 PID 的 `app.worker` 进程；m1 退出后
  `os.kill(pid, 0)` 旁证 `ESRCH`；
- 一次真实试跑中 job B 第 4 段的留痕：
  - 实算摘要：`m1:8644f8509c85` 与 `m2:8644f8509c85`（旧机与重验机结果一致）；
  - 正式结论：`verified by m2@tick17`，且全段仅此一条；
  - 拒绝审计按时间依次为
    `machine_exited@tick10`（停机后放出延迟箱）→
    `conclusion_exists@tick17`（m2 入账后重放）→
    `generation_fenced`（m1 以新代次重启后用旧代次重放）。

> 说明：本开发沙箱**没有 docker / podman 运行时**（无 CLI、无 `/var/run/docker.sock`），
> 因此 `docker compose up --build` 没能在本机实际执行；`docker-compose.yml` 已通过
> YAML 解析与字段核对，镜像构建步骤等价于已验证的本机 pip 安装 +
> `python -m app.volume` + `python -m app.main`。这是明确的未实测项。

---

## 7. 缺项与边界（演练定位，不是生产系统）

- **Docker 运行时未在交付环境实测**（见上），只做了文件/YAML 静态校验；
- “耗时 N 拍”是**逻辑演练时间**，由 `/clock/advance` 手工推进；真实 SHA-256 是
  立即实算的（256 KiB 段约 0.2–0.4 ms），并不真的睡 N 拍——这样既能真实核对摘要，
  又能让交接时序确定、可重复；
- 两台 Worker 由**同一个 FastAPI 进程内的 Supervisor 以子进程方式监管**（确为两个独立
  OS 进程、独立 PID/内存），但不是两台物理机/两个独立容器；跨主机退机需要额外的
  心跳/租约超时与外部进程看护；
- 没有鉴权、多租户、TLS、分页和审计签名；`lease_token` 会在 `GET /machines/{id}`
  明文展示，仅为本地演练透明；
- 样例卷默认干净，主演练全部 `verified`；`mismatch` 路径由 `selftest_ledger.py`
  覆盖，未在主演练里注入坏段（可自行用错误摘要打 `/results` 或重建带篡改的卷）；
- SQLite 适合单机演练，不是高可用账本；
- 未提供浏览器自动化截图，`/docs` 的可用性通过 OpenAPI/HTTP 与脚本覆盖。
