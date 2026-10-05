# 实验室仪器校准与方法验证

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8309`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制、放行依赖快照登记与级联失效、旧数据回填。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `src/maintenance.py`：后台分批回填与依赖重算线程。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则、依赖级联和失败场景测试。

## 依赖重算链路

检测结果不再只按"当场状态"放行，而是登记一条可重算的依赖依据：

1. **放行登记**：`result` 放行（`release`）时，系统校验仪器在用、存在当前批准且未到期的校准（取该仪器最新一条 `approved` 且 `due_at` 覆盖当天的校准）、方法已验证且覆盖该仪器，并把仪器/校准/方法的**ID 与版本号**、校准到期日快照写入 `release_records`。原始测量值（`measurement`）保留在结果上不变。
2. **变更即失效**：校准被撤销（`calibration.revoke`）、校准被重新批准或仪器重新校准产生新版本、方法被吊销（`method.revoke_method`）、仪器被隔离（`instrument.quarantine`）后，同一事务内所有仍依赖它的放行结果立即从 `released` 转为 `pending_review`（待复核），对应放行记录置为非活跃并写明 `invalid_reason`，审计写入 `dependency_invalidated`。
3. **重新放行**：待复核结果必须按**当前有效**的校准和方法重新通过校验才能再次 `release`；每次放行都追加一条 `release_records`，旧记录置非活跃但保留，因此历次放行与失效原因均可追溯。
4. **到期重算**：到期日跨越没有变更事件，由重算 `POST /api/maintenance/recalculate`（或后台维护线程周期执行）按当前日期重评所有活跃放行，到期校准同样使结果进入待复核。
5. **旧数据回填**：升级前已放行、没有依赖登记的结果，由 `POST /api/maintenance/backfill` 或后台线程**按仪器当前状态**分批回填（默认每批 100 条、每条独立短事务），不阻塞新结果放行；回填时已失效的结果直接转待复核。
6. **并发先后**：所有写动作在单个 `BEGIN IMMEDIATE` 事务内完成并串行化。同一台仪器的撤销与结果放行同时提交时，先拿到写锁并提交者生效：撤销先到则放行被拒（仪器已非在用），放行先到则撤销在同一顺序内把结果级联为待复核。

### 结果状态

- `pending`：待检测/待放行；`released`：已放行（存在活跃放行记录）；`pending_review`：依赖失效待复核；`blocked`：被封存（可 `reanalyze` 回到 pending）。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8309
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。
`--maintenance-interval` 设置后台回填/重算轮询秒数（默认 300，`0` 为不启动），
`--backfill-batch` 设置每轮回填条数。

## 核心对象

- `instrument`：仪器状态；`calibration`：校准记录；`method`：方法版本；`result`：检测结果。
- `release_records`：每次放行一条的依赖快照与历次放行/失效历史。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/entities/<id>/releases`：读取结果的历次放行记录与依赖版本。
- `POST /api/maintenance/recalculate`：按当前日期（或 body 中 `as_of`）重算所有活跃放行，仅 admin。
- `POST /api/maintenance/backfill`：回填一批旧放行结果（body 可带 `batch_size`、`as_of`），仅 admin。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

校准周期、误差和放行规则是可演示的业务模型，不替代实验室质量体系或计量认证。
