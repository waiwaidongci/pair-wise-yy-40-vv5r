# 建筑抗震鉴定与加固排序

依据结构、用途、人员密度和历史缺陷生成鉴定与加固优先级。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/settlement_rules.py`：施工结算判定（超合同、缺签证、重复期）与结算角色矩阵。
- `src/settlement_service.py`：报量提交、核对确认、更正重算、支付留档的用例编排。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
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

加固施工按节点报量：施工单位提交本期工程量和合同，系统按结算判定规则核对——累计报量超过合同量（合同量+签证量）、缺签证或同一期重复提交，即停在待确认并说明原因；核对通过后写入付款计划。合同量或签证更正后，未付计划按新值重算，已付记录留档不变。列表返回待确认、已核准和累计金额；预算员（budget_officer）只能看到备注。

- `POST /api/settlements`：提交报量（contractor），首次随附`contract`，后续用`contract_id`
- `GET /api/settlements?contract_id={id}`：待确认、已核准、付款计划和累计金额
- `POST /api/settlements/{id}/confirm`：待确认报量核对（settlement_admin）
- `POST /api/contracts/{id}/correction`：合同量/签证/单价更正，必须提交`expected_version`，未付计划自动重算
- `POST /api/payment-plans/{id}/pay`：支付并留档
- `GET /api/contracts`、`GET /api/contracts/{id}`

结算角色：contractor（提交）、settlement_admin（核对、更正、支付）、budget_officer（仅备注）、viewer。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
