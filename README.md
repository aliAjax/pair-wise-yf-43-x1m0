# 实验室仪器校准与方法验证

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8309`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8309
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `instrument`：仪器状态；`calibration`：校准记录；`method`：方法版本；`result`：检测结果。

## 依赖重算

结果与仪器、校准、方法之间按版本登记依赖，上游失效会立即重算下游结果：

- **放行登记**：结果放行（`release`）时在同一事务内登记当前有效的仪器、校准记录和方法版本快照（`data.releases`，含 `instrument_version`、`calibration_id/version/due_at`、`method_version`、放行人和时间），并校验仪器 `active`、校准在有效期内、方法 `validated` 且覆盖该仪器。原始测量值 `measurement` 始终保留。
- **上游失效立即重算**：校准撤销（`revoke`）、方法吊销（`revoke_method`）、仪器隔离（`quarantine`）在同一事务内把依赖它的已放行结果置为 `review`（待复核），并写入 `invalidate` 审计，原因可查。
- **重新放行**：待复核结果再次 `release` 时按当前有效校准和方法重新校验，通过后追加一条放行记录（历次放行记录留存可查），不通过则保持待复核。
- **到期扫描**：`POST /api/results/recalculate` 扫描已放行结果，校准到期或依赖失效的转入待复核。
- **回填**：`POST /api/results/backfill` 对没有依赖登记的历史已放行结果，按仪器当前状态回填依赖快照；依赖已失效的转入待复核。回填幂等（已登记的跳过）、按小批量短事务提交，不打扰新结果放行。
- **先到的生效**：所有状态变更在 `BEGIN IMMEDIATE` 事务内完成，放行在事务内重校依赖状态，消除「先查后写」的竞态；放行数据可带 `expected_versions`（`{"instrument": n, "calibration": n, "method": n}`）做依赖乐观锁，依赖已被先到的操作改动时返回 `409 ConflictError`。

结果状态机：`pending` → `released` → `review`（待复核）→ 重新 `release`；`pending` → `blocked` → `pending`。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `POST /api/results/backfill`：回填历史结果依赖快照（仅管理员）。
- `POST /api/results/recalculate`：扫描并失效到期/失效依赖的已放行结果（仅管理员）。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

校准周期、误差和放行规则是可演示的业务模型，不替代实验室质量体系或计量认证。
