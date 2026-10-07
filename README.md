# 公共采购密封投标与评审系统

标准库实现的招标发布、密封投标、开标校验收、规则评分、利益冲突、澄清、废标、投诉重评、限时反向竞价和授标快照服务。

## 运行

要求 Python 3.11+（当前 Python 3.9 环境亦可）。

```bash
python3 app.py --init --seed
python3 app.py
```

默认地址 `http://127.0.0.1:8209`，数据库默认 `public_procurement.db`。

## 主要接口

使用 `X-User`、`X-Role` 请求头。角色有 `procurement`、`vendor`、`evaluator`、`supervisor`、`auditor`、`public`。

- `GET /health`、`GET /api/state`、`GET /api/tenders/{id}`
- `POST /api/vendors`、`POST /api/tenders`、`POST /api/tenders/publish`
- `POST /api/bids`、`POST /api/bids/withdraw`、`POST /api/bids/disqualify`
- `POST /api/tenders/open`：截止后开标并核验承诺哈希
- `POST /api/conflicts`、`POST /api/evaluations`
- `POST /api/clarifications`、`POST /api/clarifications/answer`
- `POST /api/complaints`、`POST /api/complaints/resolve`
- `POST /api/auctions/start`：开标后发起限时反向竞价
- `POST /api/auctions/offers`：供应商降价报价（需幂等键与投标版本）
- `GET /api/tenders/{id}/auction`：当前最低价、结束时间、排名、报价与竞价事件
- `POST /api/auctions/recover`：恢复计时并结算到期竞价（服务重启时自动执行）
- `POST /api/tenders/award`：锁定评分轮次并保存排名快照；存在竞价时须等竞价结束，按冻结的成交快照授标

## 反向竞价规则

- 只有已开标、未废标且无利益冲突的供应商才能进入竞价。
- 每次报价必须低于当前最低价，并按投标版本做乐观并发校验；冲突或出价不够低时返回 409，响应中带回当前最低价与结束时间，后到者保留输入重新出价。
- 最后两分钟内出现有效降价，结束时间自动顺延两分钟；同一幂等键重复提交直接返回首次结果，不会再次加时。
- 最低价每次更新后，旧评分排名立即失效：价格分按当前最低价为基准重算，排名版本递增并记录竞价事件。
- 结束时间持久化在数据库中，后台计时器到期即结算；服务崩溃重启后自动恢复计时与竞价事件，到期竞价立即补结算。
- 竞价结束时冻结成交快照（最终排名、最低价、成交人），之后报价被拒绝；竞价未结束前采购方不能授标，结束后按快照授标。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整开标授标、截止前正文隐藏、利益冲突、重复评分覆盖、投诉重评、角色权限，以及反向竞价的进入校验、低价与版本校验、并发报价冲突、最后两分钟顺延、幂等重放、排名重算、崩溃恢复和授标门禁。

## 局限

供应商与请求用户没有绑定校验，身份仍依赖请求头；投标正文虽然按接口阶段隐藏，但数据库本身未加密；评分规则适合演示，不覆盖复杂资格预审、保证金、电子签名和采购法规差异。
