# 建筑抗震鉴定与加固排序

依据结构、用途、人员密度和历史缺陷生成鉴定与加固优先级。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/settlement_rules.py`：施工结算判定（待确认原因、金额与重算规则）和结算角色矩阵。
- `src/settlement_repository.py`：结算进度存档（合同、签证、报量、付款计划、已付记录）。
- `src/settlement_service.py`：结算用例编排、权限检查和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8317
```

默认端口为`8317`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/audit`

允许角色：assessor, structural_engineer, review_board, viewer。风险分值和人员密度共同影响排序；审核通过前必须完成评估、设计和施工证据登记。

## 施工结算

加固施工按节点报量：施工单位提交本期工程量和合同，完成量、签证和已付金额集中存档，避免超额支付。

- 结算判定（`src/settlement_rules.py`）：累计报量超过合同量（合同量+签证增减量）、缺签证或同一期重复提交的报量单停在待确认并写明原因；核对通过后自动写入付款计划。重复判定只看待确认/已核准中先于本单提交的报量。
- 进度存档（`src/settlement_repository.py`）：合同、签证、报量、付款计划、已付记录五张表，判定和重算在事务内完成。
- 接口入口（`src/http_api.py`）：`/api/settlement/*` 路由统一入口。
- 合同量或签证更正后，未付计划按新值重算；已付计划与已付记录留档不变。待确认报量可在更正后重新核对。
- 结算角色：contractor（施工单位）、budget_officer（预算员）、finance（财务）、viewer。

### 结算接口

- `POST /api/settlement/contracts`，`GET /api/settlement/contracts`
- `POST /api/settlement/contracts/{id}/correct`，更正合同量/单价并重算未付计划，须提交`expected_version`
- `POST /api/settlement/contracts/{id}/visas`，`POST /api/settlement/visas/{id}/correct`
- `POST /api/settlement/reports`，提交本期报量并自动判定
- `POST /api/settlement/reports/{id}/recheck`，更正后重新核对，须提交`expected_version`
- `POST /api/settlement/plans/{id}/pay`，支付并写入已付留档，须提交`expected_version`
- `GET /api/settlement/reports`、`GET /api/settlement/plans`、`GET /api/settlement/payments`
- `GET /api/settlement/summary`，列表展示待确认、已核准和累计金额

## 测试

```bash
python3 -m unittest discover -s tests -v
```
