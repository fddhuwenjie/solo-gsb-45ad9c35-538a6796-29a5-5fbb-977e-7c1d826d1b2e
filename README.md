# 乐曲速度图换算 API（Tempo Map Converter）

用 **Python + FastAPI** 实现的乐曲速度图换算服务：在乐谱位置
`小节:拍:tick` 与时间（秒）之间双向换算，支持 **PPQ**、**只在小节首拍
生效的拍号表**，以及 **阶梯（step）/ 线性（linear）BPM** 速度点。

此外提供**带录音锚点的速度图修订发布**：创建谱例时冻结各锚点按原图计算
的秒数与“该位置一个 tick 的时长”，可凭**修订号（乐观锁）**预览、发布
整份替换速度点表；发布时锁定锚点偏移超过录入时一 tick 即 **422**，
修订号过期即 **409**；数据保存在 **SQLite**，Compose 重启后仍可读取。

还可为已保存的谱例安排**排练提示**：创建排练时提交若干提示的乐谱位置
与文字（位置随谱例拍号校验），客户端用**从排练开始累计的非负秒数**
领取到点提示；每次领取按谱例**当前已提交的速度图**给未发提示定时，
返回提示文字、位置、触发秒数与所用修订号；**进度只前进**（重复领取
不重发、倒退 **409**），发布后**已发提示不撤回、未发提示按新修订
重新定时**；排练中途可**定位（跳转）**到另一乐谱位置：提交当前已
领取的累计秒数（必须与进度精确相等，否则 **409** 且状态不变）与目标
`小节:拍:tick`，尚未发放且位于目标之前的提示记为**跳过**（目标处
提示可在同一秒领取，回跳不重发、不恢复），此后提示按“提交秒数 +
目标到提示的速度图时长”定时；发布后待发提示按最新修订重新计时，
而已发记录、跳过状态与定位起点不变。排练、发放与跳过记录同样保存在
SQLite，重启后可继续领取。

- 位置中的“拍”按**当前拍号分母**对应的音符计（6/8 中一拍 = 八分音符）；
- 内部统一展开为“距乐谱起点的 **tick 坐标**”（PPQ：每四分音符的 tick 数）；
- 线性速度段按解析积分换算，反向换算在**同一速度段内解析求逆**；
- 正反向边界归属一致（边界 tick 归后一个速度点，右连续）；
- 往返误差 ≤ **1 tick**（响应中直接给出残差与判定）；
- 非法拍号、速度点非递增、BPM 非正等情况返回 **422**，错误项带有
  出错的**数组索引** `index`。

---

## 目录

- [一键启动（Docker Compose）](#一键启动docker-compose)
- [本地直接运行](#本地直接运行)
- [接口地址与交互式文档](#接口地址与交互式文档)
- [录音锚点修订：创建 / 预览 / 发布](#录音锚点修订创建--预览--发布)
- [排练提示：创建排练 / 领取到点提示 / 定位跳转](#排练提示创建排练--领取到点提示)
- [约定与规则](#约定与规则)
- [请求字段](#请求字段)
- [响应字段](#响应字段)
- [示例](#示例)
- [错误响应（422，含索引）](#错误响应422含索引)
- [运行测试](#运行测试)
- [项目结构](#项目结构)

---

## 一键启动（Docker Compose）

需要 Docker 与 Docker Compose（v2）。

```bash
docker compose up --build
```

启动后服务监听在：

- 本机地址：<http://localhost:8000>
- 健康检查：<http://localhost:8000/health>
- 换算接口：`POST http://localhost:8000/convert`
- 创建谱例：`POST http://localhost:8000/scores`
- 预览修订：`POST http://localhost:8000/scores/{id}/revisions/{rev}/preview`
- 发布修订：`POST http://localhost:8000/scores/{id}/revisions/{rev}/publish`
- 查询谱例：`GET  http://localhost:8000/scores/{id}`
- 创建排练：`POST http://localhost:8000/rehearsals`
- 领取到点提示：`POST http://localhost:8000/rehearsals/{id}/claims`
- 定位（跳转）：`POST http://localhost:8000/rehearsals/{id}/locate`
- 查询排练：`GET  http://localhost:8000/rehearsals/{id}`
- Swagger 交互文档：<http://localhost:8000/docs>
- ReDoc：<http://localhost:8000/redoc>
- OpenAPI Schema：<http://localhost:8000/openapi.json>

谱例、锚点、排练与发放记录保存在容器内 `/data/tempo.db`（SQLite），
并挂载到命名卷 `tempo-data`：`docker compose down`（保留卷）后再 `up`，
已创建与发布的修订、已领取的进度仍可读取；如需清空，执行
`docker compose down -v`。数据库路径可用环境变量 `TEMPO_DB_PATH`
覆盖（本地裸跑默认 `./data/tempo.db`）。

停止：`docker compose down`（后台运行：`docker compose up --build -d`）。

## 本地直接运行

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

地址同上。

---

## 录音锚点修订：创建 / 预览 / 发布

适用于“已有一份速度图和若干**录音锚点**，随后想整份替换速度点表，但
不希望已锁定的录音时刻被改动”的修订发布流程。修订号采用**乐观锁**：
创建时为 `0`，每次成功发布 `+1`。

### 规则

1. **创建** `POST /scores`：提交现有 `ppq`、`time_signatures`、
   `tempo_points` 与锚点表 `anchors`（每项 `{position, locked}`，
   **至少一个 `locked=true`**）。服务端按**原图**计算并保存：
   - 每个锚点的秒数 `original_seconds`（= 创建时的 `current_seconds`）；
   - 该锚点位置**录入时一个 tick 的时长** `tick_seconds`（线性段也按
     同一套解析积分计时计算），作为日后锁定偏移的阈值。

   返回谱例 `id` 与修订号 `revision: 0`。
2. **预览** `POST /scores/{id}/revisions/{expected_revision}/preview`：
   提交**整份替换**速度点表（PPQ、拍号沿用创建时的值，校验同
   `/convert`）。返回每个锚点的 `original_seconds`、`new_seconds`、
   `offset_seconds`（new − original，带符号）与 `lock_conflict`，
   以及整体 `has_lock_conflict`。**预览不修改任何已存数据**；
   `expected_revision` 与当前修订号不一致时返回 **409**。
3. **发布** `POST /scores/{id}/revisions/{expected_revision}/publish`：
   按**当前修订**重新计算：
   - 任一**锁定**锚点新秒数相对创建时保存的秒数，偏移绝对值
     **严格超过**该锚点录入时一 tick 的时长（`tick_seconds`），
     返回带锚点 `index` 的 **422**，**数据不变**（偏移恰好等于
     一 tick 不视为超过，允许发布）；
   - 成功则在**单事务**内一次更新速度图、**未锁定**锚点的
     `current_seconds` 与修订号；**锁定锚点秒数保持创建时的值不变**。
4. **并发**：预览后若已有其他发布，旧的预期修订号再次发布/预览仍返回
   **409**（响应体含 `expected_revision` 与 `current_revision`），
   不会覆盖新版本。
5. **查询** `GET /scores/{id}`：返回**同一版本**的速度图、修订号与锚点
   （锁定秒数、未锁定当前秒数、冻结阈值）。服务端在**单个读事务**内
   一次读出谱例行与全部锚点行（共用同一已提交快照），因此与发布交错时
   结果要么是发布前的**完整旧修订**，要么是发布后的**完整新修订**，
   不会出现“旧修订号/旧速度点配上新修订锚点秒数”的跨修订拼接
   （WAL 下读不阻塞写，并发发布照常提交）。
6. 数据保存到 **SQLite**（WAL 模式，发布用 `BEGIN IMMEDIATE` 串行化），
   Docker Compose 命名卷持久化，**重启后仍可读取**。

### HTTP 调用示例

> 下列示例直接从**实际返回的 JSON 中提取 ID**（需要 `python3`；
> 也可用 `jq -r .id`），按顺序粘贴即可在全新服务上完整跑通；
> 响应里的 ID 每次都不同，示例 JSON 中的 ID 仅为某次实际运行的返回值。

#### 1）创建谱例（返回 ID 与修订号 0）

```bash
SID=$(curl -s -X POST http://localhost:8000/scores \
  -H 'Content-Type: application/json' \
  -d '{
    "ppq": 480,
    "time_signatures": [],
    "tempo_points": [
      {"position": "1:1:0", "bpm": 120, "curve": "step"}
    ],
    "anchors": [
      {"position": "1:1:0", "locked": true},
      {"position": "2:1:0", "locked": false}
    ]
  }' | python3 -c 'import sys,json; print(json.load(sys.stdin)["id"])')
echo "谱例 ID：$SID"
```

120 BPM、PPQ=480 下一拍 0.5 s、一 tick `0.5/480` s；`2:1:0` 即一小节
后 = 2.0 s：

```json
{
  "id": "ac0224ce71d34c90bbe6f3a917602bbe",
  "revision": 0,
  "ppq": 480,
  "tempo_points": [{"position": "1:1:0", "bpm": 120.0, "curve": "step"}],
  "anchors": [
    {"index": 0, "position": "1:1:0", "locked": true,
     "total_ticks": 0, "original_seconds": 0.0,
     "current_seconds": 0.0, "tick_seconds": 0.0010416666666666667},
    {"index": 1, "position": "2:1:0", "locked": false,
     "total_ticks": 1920, "original_seconds": 2.0,
     "current_seconds": 2.0, "tick_seconds": 0.0010416666666666075}
  ]
}
```

#### 2）预览整份替换速度点表（不改数据）

把整体速度放慢到 60 BPM（`2:1:0` 由 2.0 s 变 4.0 s）：

```bash
curl -s -X POST http://localhost:8000/scores/$SID/revisions/0/preview \
  -H 'Content-Type: application/json' \
  -d '{
    "tempo_points": [{"position": "1:1:0", "bpm": 60, "curve": "step"}]
  }'
```

```json
{
  "id": "ac0224ce71d34c90bbe6f3a917602bbe",
  "expected_revision": 0,
  "current_revision": 0,
  "has_lock_conflict": false,
  "anchors": [
    {"index": 0, "position": "1:1:0", "locked": true,
     "original_seconds": 0.0, "new_seconds": 0.0,
     "offset_seconds": 0.0, "lock_conflict": false,
     "tick_seconds": 0.0010416666666666667},
    {"index": 1, "position": "2:1:0", "locked": false,
     "original_seconds": 2.0, "new_seconds": 4.0,
     "offset_seconds": 2.0, "lock_conflict": false,
     "tick_seconds": 0.0010416666666666075}
  ]
}
```

> 若锁定锚点（如把 `2:1:0` 设为 `locked=true`）在新图下偏移超过一 tick，
> 预览仍返回 **200**，但对应锚点 `lock_conflict=true`、
> 顶层 `has_lock_conflict=true`；此时发布会被拒绝。

#### 3）发布（修订号 0 → 1）

```bash
curl -s -X POST http://localhost:8000/scores/$SID/revisions/0/publish \
  -H 'Content-Type: application/json' \
  -d '{
    "tempo_points": [{"position": "1:1:0", "bpm": 60, "curve": "step"}]
  }'
```

成功返回更新后的谱例（`revision` 变为 `1`）：**未锁定**锚点
`2:1:0` 的 `current_seconds` 更新为 `4.0`；**锁定**锚点 `1:1:0`
秒数保持 `0.0`，其冻结的 `original_seconds` / `tick_seconds` 不变。

随后用**旧预期修订号**再发一次（哪怕中间只做过预览）会得到 **409**：

```bash
curl -i -X POST http://localhost:8000/scores/$SID/revisions/0/publish \
  -H 'Content-Type: application/json' \
  -d '{"tempo_points": [{"position": "1:1:0", "bpm": 240, "curve": "step"}]}'
# HTTP/1.1 409 Conflict
# {"detail": [{"loc": ["path", "expected_revision"],
#   "msg": "修订号冲突：预期 0，当前 1", "index": null,
#   "expected_revision": 0, "current_revision": 1}]}
```

锁定冲突时返回带锚点索引的 **422**（数据保持原样、修订号不前进）：

```json
{
  "detail": [
    {"loc": ["body", "anchors", 1],
     "msg": "锁定锚点 1（2:1:0）相对创建时保存的 2.000000s 偏移 +2.000000s，超过该位置录入时一 tick 的时长 0.001041667s",
     "index": 1}
  ]
}
```

#### 4）查询同一版本的速度图与锚点

```bash
curl -s http://localhost:8000/scores/$SID
```

上一步发布成功后，返回的 `revision`、`tempo_points` 与每个锚点的
`current_seconds` 必定描述**同一次修订**（rev1 / 60 BPM / 4.0 s）：

```json
{
  "id": "ac0224ce71d34c90bbe6f3a917602bbe",
  "revision": 1,
  "ppq": 480,
  "time_signatures": [],
  "tempo_points": [{"position": "1:1:0", "bpm": 60.0, "curve": "step"}],
  "anchors": [
    {"index": 0, "position": "1:1:0", "locked": true,
     "total_ticks": 0, "original_seconds": 0.0,
     "current_seconds": 0.0, "tick_seconds": 0.0010416666666666667},
    {"index": 1, "position": "2:1:0", "locked": false,
     "total_ticks": 1920, "original_seconds": 2.0,
     "current_seconds": 4.0, "tick_seconds": 0.0010416666666666075}
  ]
}
```

要点：

- `revision` 与 `tempo_points`、所有 `anchors[*]` 取自**同一已提交快照**；
  即使查询过程中恰好有发布提交，也只会整体返回旧修订或整体返回新修订，
  绝不会返回“旧修订号 + 新锚点秒数”的拼接结果（例如 rev0 / 120 BPM
  却带上 rev1 / 60 BPM 才有的 `4.0` s）。
- `original_seconds` / `tick_seconds` 是创建时冻结值，任何修订都不变；
  锁定锚点的 `current_seconds` 也保持冻结值，未锁定锚点随发布更新。
- 谱例不存在时返回 **404**。

---

## 排练提示：创建排练 / 领取到点提示

适用于“为已保存的谱例安排排练提示，客户端按排练累计秒数领取到点提示”
的场景。提示位置在创建排练时校验并展开为 tick 坐标（PPQ/拍号沿用谱例
创建时的值，不随速度图修订变化）；**触发秒数在每次领取时**按谱例
**当前已提交的速度图**现算。

### 规则

1. **创建排练** `POST /rehearsals`：提交 `score_id` 与提示表 `cues`
   （每项 `{position, text}`，至少一个，文字去空白后非空）。位置按谱例
   的 PPQ/拍号校验（与 `/convert`、锚点同一套规则），非法位置/空文字
   返回带 `index` 的 **422**；谱例不存在返回 **404**。返回排练 `id`
   与全部提示（含展开后的 `total_ticks`）。
2. **领取** `POST /rehearsals/{id}/claims`：提交从排练开始累计的
   `elapsed_seconds`（非负、有限，否则 **422**）。服务端在**单个写事务**
   内读出谱例当前已提交的速度图与修订号，计算全部**未发**提示的触发
   秒数，发放 `trigger_seconds <= elapsed_seconds` 的提示，返回提示
   文字、位置、触发秒数与**所用修订号**，按**触发秒数及录入顺序**排列。
3. **进度只前进**：`elapsed_seconds` 小于已领取进度返回 **409**
   （响应体含 `last_elapsed_seconds`，**状态不变**）；等于已领取进度
   可重复领取，但**已发提示不会重发**（若发布把未发提示提前到进度内，
   会在此刻补发）。
4. **发布后重定时**：速度图发布后，**已发提示不撤回**（发放记录中的
   触发秒数与修订号保持发放时的值）；**未发提示**在下一次领取时按
   **新修订**重新定时。领取的读-算-写在同一事务内完成，与发布交错时
   二者必居其一，**计算到点所用的修订必然等于写入发放记录的修订**；
   并发领取被串行化，同一提示**最多发放一次**（发放记录主键兜底）。
5. **定位（跳转）** `POST /rehearsals/{id}/locate`：排练中途跳到另一
   乐谱位置时提交当前**已领取的累计秒数** `elapsed_seconds` 与目标
   `position`（`小节:拍:tick`，按谱例 PPQ/拍号校验）。
   - 提交秒数必须与当前已领取进度**精确相等**，否则返回 **409**
     （响应体含 `elapsed_seconds` / `last_elapsed_seconds`），计时起点、
     跳过与发放状态**一律不变**；
   - 尚未发放且 tick **严格位于目标之前**的提示记为**跳过**
     （记录定位时的修订号）；tick 等于目标（目标处）的提示**不跳过**，
     触发秒数恰为提交秒数，**可在同一秒领取**；
   - 每次定位以（提交秒数, 目标 tick）更新**计时起点**；后续提示的
     触发秒数 = `提交秒数 + 速度图上目标位置到提示位置的时长`
     （线性段同样按解析积分计）。未做过定位的排练起点是
     `1:1:0 / 0.0s`，触发秒数即全图绝对秒数，保持原有计时；
   - **回跳不重发**已发提示，也**不恢复**已跳过提示；回跳后已位于
     目标之前的待发提示（若有）同样记为跳过；
   - 定位的读-判-写在**单个写事务**内完成，与领取、发布共用写锁
     串行化：同一条提示不可能既跳过又发放，跳过记录的修订必然是
     定位瞬间谱例的当前修订。
6. **发布与定位的关系**：速度图发布后，**待发提示按最新已提交修订
   重新计时**（起点 tick 到提示 tick 的速度图时长随新图变化）；
   **已发记录、跳过状态与定位起点（tick/提交秒数）都不变**。
7. **查询** `GET /rehearsals/{id}`：返回已领取进度、当前计时起点
   `origin`（位置/tick/累计秒数）与每条提示的状态
   `status`（`pending` 待发 / `delivered` 已发 / `skipped` 跳过；
   同时保留兼容字段 `delivered`、已发提示的 `trigger_seconds` /
   `revision`，以及跳过提示的 `skipped_revision`）。
8. 排练、提示、发放与跳过记录保存在 **SQLite**（`rehearsals` /
   `rehearsal_cues` / `cue_deliveries` / `cue_skips` 表，与谱例同库同卷；
   旧库打开时自动补定位起点列），Compose 重启后可继续领取。

### HTTP 调用示例

以下沿用上文创建的谱例（`$SID`，120 BPM、4/4、PPQ=480：一拍 0.5 s、
一小节 2.0 s）。

#### 1）创建排练（从返回中取出排练 ID）

```bash
RID=$(curl -s -X POST http://localhost:8000/rehearsals \
  -H 'Content-Type: application/json' \
  -d '{
    "score_id": "'$SID'",
    "cues": [
      {"position": "2:1:0", "text": "进副歌"},
      {"position": "3:1:0", "text": "渐慢准备"},
      {"position": "5:1:0", "text": "收尾"}
    ]
  }' | python3 -c 'import sys,json; print(json.load(sys.stdin)["id"])')
echo "排练 ID：$RID"
```

创建响应（ID 为实际返回值，此处用占位符表示）：

```json
{
  "id": "<排练 ID>",
  "score_id": "<谱例 ID>",
  "last_elapsed_seconds": 0.0,
  "origin": {"position": "1:1:0", "total_ticks": 0, "elapsed_seconds": 0.0},
  "cues": [
    {"index": 0, "position": "2:1:0", "text": "进副歌", "total_ticks": 1920,
     "status": "pending", "delivered": false,
     "trigger_seconds": null, "revision": null, "skipped_revision": null},
    {"index": 1, "position": "3:1:0", "text": "渐慢准备", "total_ticks": 3840,
     "status": "pending", "delivered": false,
     "trigger_seconds": null, "revision": null, "skipped_revision": null},
    {"index": 2, "position": "5:1:0", "text": "收尾", "total_ticks": 7680,
     "status": "pending", "delivered": false,
     "trigger_seconds": null, "revision": null, "skipped_revision": null}
  ]
}
```

位置非法（如 `1:9:0` 超出 4/4 拍数）或文字为空时返回带 `index` 的 422。

#### 2）排练进行到 2.0 s：领取到点提示

```bash
curl -s -X POST http://localhost:8000/rehearsals/$RID/claims \
  -H 'Content-Type: application/json' \
  -d '{"elapsed_seconds": 2.0}'
```

`2:1:0` 在 120 BPM 下触发于 2.0 s（已到点），`3:1:0` 触发于 4.0 s、
`5:1:0` 触发于 8.0 s（均未到点）：

```json
{
  "id": "<排练 ID>",
  "elapsed_seconds": 2.0,
  "revision": 0,
  "cues": [
    {"index": 0, "position": "2:1:0", "text": "进副歌",
     "trigger_seconds": 2.0, "revision": 0}
  ]
}
```

#### 3）定位（跳转）到 4:1:0：跳过之前的提示，重置计时起点

排练中途跳到 `4:1:0`（绝对 6.0 s 处），提交的累计秒数必须等于当前
已领取进度 2.0 s：

```bash
curl -s -X POST http://localhost:8000/rehearsals/$RID/locate \
  -H 'Content-Type: application/json' \
  -d '{"elapsed_seconds": 2.0, "position": "4:1:0"}'
```

待发且位于目标之前的“渐慢准备”（4.0 s < 6.0 s）记为**跳过**；
“收尾”在目标之后（绝对 8.0 s，距目标 2.0 s）保持待发，触发秒数
改为 `2.0 + 2.0 = 4.0` s：

```json
{
  "id": "<排练 ID>",
  "elapsed_seconds": 2.0,
  "revision": 0,
  "origin": {"position": "4:1:0", "total_ticks": 5760,
             "elapsed_seconds": 2.0},
  "skipped": [
    {"index": 1, "position": "3:1:0", "text": "渐慢准备",
     "total_ticks": 3840, "revision": 0}
  ],
  "cues": [
    {"index": 0, "position": "2:1:0", "text": "进副歌", "total_ticks": 1920,
     "status": "delivered", "delivered": true,
     "trigger_seconds": 2.0, "revision": 0, "skipped_revision": null},
    {"index": 1, "position": "3:1:0", "text": "渐慢准备", "total_ticks": 3840,
     "status": "skipped", "delivered": false,
     "trigger_seconds": null, "revision": null, "skipped_revision": 0},
    {"index": 2, "position": "5:1:0", "text": "收尾", "total_ticks": 7680,
     "status": "pending", "delivered": false,
     "trigger_seconds": null, "revision": null, "skipped_revision": null}
  ]
}
```

定位秒数不等于当前进度（或倒退领取）返回 **409** 且状态不变：

```bash
curl -i -X POST http://localhost:8000/rehearsals/$RID/locate \
  -H 'Content-Type: application/json' \
  -d '{"elapsed_seconds": 3.0, "position": "4:1:0"}'
# HTTP/1.1 409 Conflict
# {"detail": [{"loc": ["body", "elapsed_seconds"],
#   "msg": "定位秒数必须等于当前已领取进度：本次 3.0s，已领取进度 2.0s",
#   "index": null, "elapsed_seconds": 3.0, "last_elapsed_seconds": 2.0}]}
```

目标位置非法（如 `1:9:0`）或秒数为负/非有限值返回 **422**。

#### 4）定位后继续领取：按“提交秒数 + 目标到提示时长”触发

同一秒（2.0 s）重复领取不发任何东西（没有位于目标 tick 上的提示）；
“收尾”在新计时下触发于 4.0 s：

```bash
curl -s -X POST http://localhost:8000/rehearsals/$RID/claims \
  -H 'Content-Type: application/json' \
  -d '{"elapsed_seconds": 2.0}'
# {"id": "...", "elapsed_seconds": 2.0, "revision": 0, "cues": []}

curl -s -X POST http://localhost:8000/rehearsals/$RID/claims \
  -H 'Content-Type: application/json' \
  -d '{"elapsed_seconds": 4.0}'
```

```json
{
  "id": "<排练 ID>",
  "elapsed_seconds": 4.0,
  "revision": 0,
  "cues": [
    {"index": 2, "position": "5:1:0", "text": "收尾",
     "trigger_seconds": 4.0, "revision": 0}
  ]
}
```

回跳（如再定位回 `2:1:0`）**不重发**已发的“进副歌/收尾”，也**不
恢复**已跳过的“渐慢准备”。

#### 5）发布新速度图：待发提示按新修订重新定时，起点与跳过不变

另建一个排练（`2:1:0` 先领、定位到 `4:1:0`、`5:1:0` 待发），随后
把整体速度放慢到 60 BPM（谱例修订 0 → 1）：

```bash
RID2=$(curl -s -X POST http://localhost:8000/rehearsals \
  -H 'Content-Type: application/json' \
  -d '{
    "score_id": "'$SID'",
    "cues": [
      {"position": "2:1:0", "text": "进副歌"},
      {"position": "3:1:0", "text": "渐慢准备"},
      {"position": "5:1:0", "text": "收尾"}
    ]
  }' | python3 -c 'import sys,json; print(json.load(sys.stdin)["id"])')
curl -s -X POST http://localhost:8000/rehearsals/$RID2/claims \
  -H 'Content-Type: application/json' \
  -d '{"elapsed_seconds": 2.0}' > /dev/null
curl -s -X POST http://localhost:8000/rehearsals/$RID2/locate \
  -H 'Content-Type: application/json' \
  -d '{"elapsed_seconds": 2.0, "position": "4:1:0"}' > /dev/null

curl -s -X POST http://localhost:8000/scores/$SID/revisions/0/publish \
  -H 'Content-Type: application/json' \
  -d '{"tempo_points": [{"position": "1:1:0", "bpm": 60, "curve": "step"}]}'
```

定位起点仍是 `4:1:0 / 2.0 s`、已发与跳过状态不变；60 BPM 下
`4:1:0`→`5:1:0` 的一小节时长变为 4.0 s，故“收尾”重新定时为
`2.0 + 4.0 = 6.0` s，在 6.0 s 领取（发放记录为 rev1）：

```bash
curl -s -X POST http://localhost:8000/rehearsals/$RID2/claims \
  -H 'Content-Type: application/json' \
  -d '{"elapsed_seconds": 6.0}'
```

```json
{
  "id": "<排练 ID>",
  "elapsed_seconds": 6.0,
  "revision": 1,
  "cues": [
    {"index": 2, "position": "5:1:0", "text": "收尾",
     "trigger_seconds": 6.0, "revision": 1}
  ]
}
```

#### 6）查询排练：待发 / 已发 / 跳过可区分

```bash
curl -s http://localhost:8000/rehearsals/$RID2
```

```json
{
  "id": "<排练 ID>",
  "score_id": "<谱例 ID>",
  "last_elapsed_seconds": 6.0,
  "origin": {"position": "4:1:0", "total_ticks": 5760,
             "elapsed_seconds": 2.0},
  "cues": [
    {"index": 0, "position": "2:1:0", "text": "进副歌", "total_ticks": 1920,
     "status": "delivered", "delivered": true,
     "trigger_seconds": 2.0, "revision": 0, "skipped_revision": null},
    {"index": 1, "position": "3:1:0", "text": "渐慢准备", "total_ticks": 3840,
     "status": "skipped", "delivered": false,
     "trigger_seconds": null, "revision": null, "skipped_revision": 0},
    {"index": 2, "position": "5:1:0", "text": "收尾", "total_ticks": 7680,
     "status": "delivered", "delivered": true,
     "trigger_seconds": 6.0, "revision": 1, "skipped_revision": null}
  ]
}
```

注意“进副歌”的发放记录保持发放时的修订与触发秒数（rev0 / 2.0 s），
即使速度图已切换到 60 BPM——已发提示不随发布改变；“渐慢准备”的
跳过状态与 `skipped_revision`（定位时 rev0）同样不受发布影响；
定位起点 `origin`（4:1:0 / 2.0 s）也保持定位提交时的值，发布只让
待发提示按新修订重新计算“起点到提示”的时长。

---

## 约定与规则

### 1. PPQ 与位置 `小节:拍:tick`

- `ppq`：每个**四分音符**的 tick 数（如 480）。
- 位置字符串形如 `"1:1:0"`，三个分量均为整数：
  - **小节** `measure`：从 1 开始；
  - **拍** `beat`：从 1 开始，按**该小节生效拍号的分母**计——分母为 `d`
    时一拍是 `1/d` 音符，长度 `4*PPQ/d` tick（4/4 一拍 = 四分音符 = PPQ
    tick；6/8 一拍 = 八分音符 = PPQ/2 tick）。合法范围
    `1 ≤ beat ≤ 拍号分子`，超过即 422；
  - **tick**：`0 ≤ tick < 4*PPQ/d`（单拍范围），超出即 422。
- 内部横坐标为距乐谱起点的 tick 总数（PPQ 坐标）：

  ```
  beat_ticks  = 4 * PPQ / d          # d = 该小节生效拍号的分母
  total_ticks = 该小节首拍 tick + (beat - 1) * beat_ticks + tick
  ```

- 正向展开（位置→tick）、速度点定位、反向规范化（tick→位置）与边界
  归属共用同一坐标规则：tick 恰好落在拍号区域/小节/拍边界时归后一段，
  正反互为逆运算。
- 每个速度点/换算结果都会被规范为该坐标系下的整数 tick。

### 2. 拍号表（`time_signatures`）

- 每项形如 `{"measure": 5, "numerator": 3, "denominator": 4}`，
  表示 3/4 从**第 5 小节首拍**起生效；拍号**只能在小节首拍生效**。
- 拍号表按小节号**严格递增**（不得乱序、不得同一小节重复）。
- 拍号分母必须是 **2 的幂**（2/4/8/16/32…）。
- 单拍长度与小节长度：

  ```
  beat_ticks = 4 * PPQ / denominator   # 一拍（1/denominator 音符）的 tick 数
  bar_ticks  = numerator * beat_ticks
  ```

  `4*PPQ/denominator` 必须是**整数 tick**；若在当前 PPQ 下无法整除
  （例如 PPQ=100 时的 1/32，每拍 12.5 tick），返回 422，因为该拍号
  无法在本坐标下精确表示。
- 若拍号表为空，或第一项不在第 1 小节，则开头隐含 **4/4**。
- 复合拍号按分母计拍：6/8 = 6 个八分音符拍/小节（PPQ=480 时每拍 240
  tick、每小节 1440 tick），7/8 = 7 个八分音符拍/小节（每拍 240 tick、
  每小节 1680 tick）。

### 3. 速度点（`tempo_points`）

- 每项：`{"position": "1:1:0", "bpm": 120, "curve": "step"}`。
- **首个速度点必须位于 `1:1:0`**（tick 0）。
- 位置必须**严格递增**（相等或倒序返回 422）。
- `bpm` 必须为**正数**（有限值；0、负数、NaN、Infinity 返回 422）。
- `curve`：
  - `"step"`（默认）：从该点到下一点**保持该 BPM**（阶梯速度）；
  - `"linear"`：从该点到下一点，BPM 在两点之间**线性变化**。
- 速度点 `i` 的曲线决定区间 `[tick_i, tick_{i+1})` 内的速度；
  **末个速度点之后保持其 BPM**（恒定）。
- **边界归属一致**：边界 tick `tick_{i+1}` 归属于后一个点（右连续），
  正向（位置→秒）与反向（秒→位置）使用同一边界约定。

### 4. 线性段的积分与求逆

段内速度随 tick 线性变化：

```
B(t) = b0 + (b1 - b0) * (t - t0) / span
```

- 正向（对“秒/tick”积分）：

  ```
  ΔT = 60·span / ((b1-b0)·PPQ) · ln(B(t) / b0)
  ```

- 反向（在**同一段**内解析求逆）：

  ```
  B(t) = b0 · exp(ΔT / A)，  A = 60·span / ((b1-b0)·PPQ)
  t = t0 + span · (B(t) - b0) / (b1 - b0)
  ```

  阶梯段直接按恒定 BPM 线性换算；两端 BPM 几乎相等时自动退化为恒定
  速度的等价极限。

- 秒 → 位置先得到**未量化的浮点 tick**（`raw_total_ticks`），再四舍五入
  到整数 tick；往返残差以未量化 tick 与量化 tick 之差衡量，保证
  **≤ 1 tick**。

---

## 请求字段

`POST /convert`，`Content-Type: application/json`：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `ppq` | int | 是 | 每四分音符 tick 数，≥ 1 |
| `time_signatures` | array | 否 | 拍号表，按小节首拍生效；空时整体 4/4 |
| `tempo_points` | array | 是 | 速度点表；首点须在 `1:1:0`，严格递增 |
| `position` | string | 二选一 | 待换算位置 `"小节:拍:tick"`（拍按当前拍号分母计） |
| `seconds` | number | 二选一 | 待换算时间（秒，非负） |

`time_signatures[]`：`measure`（int，≥1）、`numerator`（int，≥1）、
`denominator`（int，2 的幂）。

`tempo_points[]`：`position`（string）、`bpm`（number，>0）、
`curve`（`"step"` 或 `"linear"`，默认 `"step"`）。

> `position` 与 `seconds` **必须且只能提供一个**，否则返回 422。

## 响应字段

| 字段 | 说明 |
|---|---|
| `position` / `measure` / `beat` / `tick` | 规范后的乐谱位置及其分量 |
| `total_ticks` | 距起点的 tick 总数（PPQ 坐标，整数） |
| `seconds` | 距起点的时间（秒） |
| `input_position` / `input_seconds` | 回显本次输入（另一个为 `null`） |
| `roundtrip.raw_total_ticks` | 秒→tick 反解的未量化浮点 tick |
| `roundtrip.residual_ticks` | 往返 tick 残差（量化前后） |
| `roundtrip.residual_seconds` | 往返秒残差 |
| `roundtrip.position_roundtrip` | 往返后重新规范化的位置 |
| `roundtrip.within_one_tick` | 往返误差是否 ≤ 1 tick |

---

## 示例

### 示例 1：位置 → 秒（拍号变化 + 阶梯/线性混合）

```bash
curl -s -X POST http://localhost:8000/convert \
  -H 'Content-Type: application/json' \
  -d '{
    "ppq": 480,
    "time_signatures": [
      {"measure": 1, "numerator": 4, "denominator": 4},
      {"measure": 5, "numerator": 3, "denominator": 4}
    ],
    "tempo_points": [
      {"position": "1:1:0", "bpm": 120, "curve": "step"},
      {"position": "2:1:0", "bpm": 60,  "curve": "linear"},
      {"position": "3:1:0", "bpm": 180, "curve": "step"},
      {"position": "5:1:0", "bpm": 90,  "curve": "step"}
    ],
    "position": "6:1:0"
  }'
```

结果（节选）：第 1 小节 120 BPM=2.0s；第 2 小节 60→180 线性=
`2·ln3`≈2.1972s；第 3–4 小节 180 BPM 共 8 拍=2.6667s；第 5 小节
90 BPM=2.0s。

```json
{
  "position": "6:1:0",
  "total_ticks": 9120,
  "seconds": 8.863891244002886,
  "roundtrip": { "residual_ticks": 0.0, "within_one_tick": true }
}
```

### 示例 2：秒 → 位置（用上一步的秒数往返）

```bash
curl -s -X POST http://localhost:8000/convert \
  -H 'Content-Type: application/json' \
  -d '{
    "ppq": 480,
    "time_signatures": [
      {"measure": 1, "numerator": 4, "denominator": 4},
      {"measure": 5, "numerator": 3, "denominator": 4}
    ],
    "tempo_points": [
      {"position": "1:1:0", "bpm": 120, "curve": "step"},
      {"position": "2:1:0", "bpm": 60,  "curve": "linear"},
      {"position": "3:1:0", "bpm": 180, "curve": "step"},
      {"position": "5:1:0", "bpm": 90,  "curve": "step"}
    ],
    "seconds": 8.863891244002886
  }'
```

回到 `"position": "6:1:0"`，`"total_ticks": 9120`，`within_one_tick: true`。

### 示例 3：复合拍号 6/8 的位置语义

6/8 中“拍”是八分音符：PPQ=480 时每拍 `4*480/8 = 240` tick。`1:4:0`
即第一小节第 4 拍 = `3*240 = 720` tick；120 BPM 下每四分音符 0.5 s，
720 tick = 1.5 个四分音符 = **0.75 s**：

```bash
curl -s -X POST http://localhost:8000/convert \
  -H 'Content-Type: application/json' \
  -d '{
    "ppq": 480,
    "time_signatures": [{"measure": 1, "numerator": 6, "denominator": 8}],
    "tempo_points": [{"position": "1:1:0", "bpm": 120, "curve": "step"}],
    "position": "1:4:0"
  }'
```

```json
{
  "position": "1:4:0",
  "total_ticks": 720,
  "seconds": 0.75,
  "roundtrip": { "residual_ticks": 0.0, "within_one_tick": true }
}
```

同一拍号下，`1:7:0`（超过第 6 拍）或 `1:1:240`（tick 超出单拍范围
240）都会返回 **422**。

### 示例 4：纯线性 60→120 渐变一拍

60 BPM 线性升到 120 BPM（跨一拍/480 tick），该拍时长为 `ln 2` 秒：

```bash
curl -s -X POST http://localhost:8000/convert \
  -H 'Content-Type: application/json' \
  -d '{
    "ppq": 480,
    "time_signatures": [],
    "tempo_points": [
      {"position": "1:1:0", "bpm": 60,  "curve": "linear"},
      {"position": "1:2:0", "bpm": 120, "curve": "step"}
    ],
    "seconds": 0.6931471805599453
  }'
# -> "position": "1:2:0", "total_ticks": 480
```

也可在 <http://localhost:8000/docs> 页面上直接点 **Try it out** 调用。

---

## 错误响应（422，含索引）

所有请求体级别的语义/结构错误统一返回 HTTP **422**，`detail` 为错误
数组；每项包含：

- `loc`：字段定位（形如
  `["body","tempo_points",1,"bpm"]`，天然带数组下标）；
- `index`：出错的**数组元素索引**（顶层字段错误时为 `null`）；
- `msg`：中文错误说明。

多个错误会**一次性聚合返回**。例如首点不在 1:1:0、第 2 个速度点
BPM=0、速度点位置重复、拍号分母不是 2 的幂，会在同一个 422 中分别
带上索引 0/1/2/0：

```json
{
  "detail": [
    {"loc": ["body", "time_signatures", 0, "denominator"],
     "msg": "拍号分母必须是 2 的幂（2/4/8/16/...），收到 6", "index": 0},
    {"loc": ["body", "tempo_points", 1, "bpm"],
     "msg": "BPM 必须是正数，收到 0.0", "index": 1},
    {"loc": ["body", "tempo_points", 0, "position"],
     "msg": "首个速度点必须位于 1:1:0（tick 0），收到 '1:2:0'", "index": 0},
    {"loc": ["body", "tempo_points", 2, "position"],
     "msg": "速度点位置必须严格递增：第 2 点 2:1:0 不晚于第 1 点 2:1:0",
     "index": 2}
  ]
}
```

会触发 422 的典型情况：PPQ < 1；拍号分母非 2 的幂；拍号小节号非严格
递增；拍号在当前 PPQ 下单拍长度非整数 tick（`4*PPQ/分母` 不可整除）；
首速度点不在 `1:1:0`；速度点位置非严格递增；BPM ≤ 0（或非有限值）；
`curve` 非法；位置格式错误、拍超出当前拍号分子或 tick 超出单拍范围；
`position`/`seconds` 同时提供或都不提供；`seconds` 为负或非有限值。

修订接口还可能返回：

- **404**：谱例 ID 不存在（`detail[0].loc` 指向 `path.score_id`）；
- **409**：路径中的 `expected_revision` 与当前修订号不一致（预览后已有
  其他发布，旧修订号仍须 409，不能覆盖新版本）：

  ```json
  {"detail": [{"loc": ["path", "expected_revision"],
               "msg": "修订号冲突：预期 0，当前 1", "index": null,
               "expected_revision": 0, "current_revision": 1}]}
  ```

- 发布修订时若**锁定锚点**相对创建时保存秒数的偏移**严格超过**
  “该位置录入时一个 tick 的时长”，按上述 422 格式返回并带锚点 `index`，
  速度图、锚点秒数与修订号均不变。

排练接口还可能返回：

- **404**：谱例 ID（创建排练时）或排练 ID（领取/定位/查询时）不存在，
  `detail[0].loc` 分别指向 `path.score_id` / `path.rehearsal_id`；
- **409**：领取秒数早于已领取进度（倒退），状态不变：

  ```json
  {"detail": [{"loc": ["body", "elapsed_seconds"],
               "msg": "领取进度只能前进：本次 1.0s 早于已领取进度 2.5s",
               "index": null,
               "elapsed_seconds": 1.0, "last_elapsed_seconds": 2.5}]}
  ```

  定位提交秒数不等于当前已领取进度同样返回 **409**（计时起点、跳过与
  发放状态均不变）：

  ```json
  {"detail": [{"loc": ["body", "elapsed_seconds"],
               "msg": "定位秒数必须等于当前已领取进度：本次 3.0s，已领取进度 2.0s",
               "index": null,
               "elapsed_seconds": 3.0, "last_elapsed_seconds": 2.0}]}
  ```

- **422**：提示位置非法或文字为空（带提示 `index`）、`elapsed_seconds`
  为负或非有限值、提示表为空；定位目标 `position` 非法（按谱例 PPQ/
  拍号校验）或定位秒数为负/非有限值。

---

## 运行测试

```bash
pip install -r requirements-dev.txt
pytest -q
```

测试覆盖：分母计拍坐标展开（含 6/8 复合拍号）、阶梯/线性速度（解析式与
Simpson 数值积分交叉校验，误差 ~1e-14）、同段反求与边界归属、拍号切换、
密集/随机点上的 ≤1 tick 往返、以及各类带索引的 422。

修订相关测试（`tests/test_scores.py`）覆盖：创建冻结锚点秒数与一 tick
阈值、预览返回原/新秒数与偏移且不落库、预览/发布的修订号 409、锁定冲突
422（带锚点索引且数据不变）、偏移恰好等于一 tick 放行、成功发布原子更新
速度图/未锁定锚点/修订号且锁定秒数不变、按当前修订连续发布、404，以及
用独立 SQLite 文件重新打开（模拟 Compose 重启）后数据仍可读。另有
**可控读写交错回归**：在 `GET /scores/{id}` 读谱例行与读锚点行之间，由
另一连接真实提交一次发布，断言返回的修订号、速度点与全部锚点秒数取自
同一快照（完整旧修订或完整新修订），不发生跨修订拼接。

排练提示测试（`tests/test_rehearsals.py`）覆盖：创建校验位置/文字并返回
排练 ID（404/422 带索引）、到点领取与按触发秒数及录入顺序排序、边界
触发时刻、负/非有限秒数 422、同一时刻重复领取不重发、倒退 409 且状态
不变、发布后已发不撤回而未发按新修订重新定时（发放记录修订与计算所用
修订一致）、同刻补发被提前的未发提示、**多连接并发领取同一提示最多
发放一次**，以及新 Store 实例（模拟 Compose 重启）后进度保留、可继续
领取。定位相关测试覆盖：目标之前待发提示记为跳过而目标处提示同秒可领、
定位后触发秒数 = 提交秒数 + 目标到提示的速度图时长（含线性段与阶梯段）、
未定位排练保持原绝对计时、前跳/直接跳转重标待发提示、回跳不重发也不
恢复、提交秒数不等于进度 409 且状态不变、目标位置/秒数非法 422、
发布后待发提示按新修订重算而定位起点/已发/跳过不变、**多连接定位与
领取交错时同一提示不会既跳过又发放（且落后的定位 409 不改状态）**、
查询可区分 pending/delivered/skipped，以及定位起点与跳过状态重启后
保留、可继续领取；另含旧版 SQLite 库（无定位起点列）打开时自动迁移、
旧排练仍按绝对秒数计时的验证。

## 项目结构

```
.
├── app/
│   ├── __init__.py
│   ├── main.py        # FastAPI 路由、请求/响应模型、422/409/404 错误处理
│   ├── tempo.py       # 核心引擎：拍号展开、积分换算、同段求逆、校验
│   └── db.py          # SQLite 持久化：谱例/锚点/修订号乐观锁原子发布，
│                      #   排练/提示/发放/跳过记录，单事务领取与定位
├── tests/
│   ├── conftest.py    # 测试用临时 SQLite（TEMPO_DB_PATH）
│   ├── test_tempo.py  # 引擎数值/往返/校验测试
│   ├── test_api.py    # /convert 端到端测试
│   ├── test_scores.py # 修订创建/预览/发布/查询与持久化测试
│   └── test_rehearsals.py # 排练创建/领取/定位跳过/并发与持久化测试
├── Dockerfile
├── docker-compose.yml # 命名卷 tempo-data -> /data（TEMPO_DB_PATH）
├── requirements.txt
└── README.md
```
