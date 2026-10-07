# 公共采购密封投标与评审系统

标准库实现的招标发布、密封投标、开标校验收、规则评分、利益冲突、澄清、废标、投诉重评、可恢复反向竞价和授标快照服务。

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
- `POST /api/auctions`：开标评审后启动限时反向竞价（参数 `tender_id`、`duration_minutes`，可覆盖顺延窗口/秒数）
- `POST /api/auctions/quote`：提交降价（`auction_id`、`bid_id`、`price`、`expected_version`、`client_token`）
- `POST /api/auctions/close`、`GET /api/auctions/{id}`
- `POST /api/tenders/award`：竞价进行中禁授标；有已收盘竞价时按成交快照锁定排名并保存授标快照

### 反向竞价规则

- **准入名册**：启动竞价时冻结，仅纳入已开标且未废标（`opened/qualified`）、无任何利益冲突记录、且已完成全部评审计分的供应商。名册冻结后不能再评分或废标。
- **报价**：必须严格低于当前最低价；按投标行 `version` 做乐观并发校验。并发窗口中后到的一笔被拒时，错误详情保留其 `entered_price` 并回传 `current_lowest_price`、领先投标、新 `ranking_epoch`，供立即改价重试。
- **防重加时**：每个客户端必须带 `client_token`，同令牌重复提交返回首次结果（`replayed: true`），不新增序号、不再顺延。
- **最后两分钟顺延**：成交瞬间剩余时间 ≤ `extension_window_seconds`（默认 120 秒）时，结束时间顺延 `extension_seconds`（默认 120 秒）；新窗口内再次有效降价可继续顺延。
- **排名失效重算**：每笔有效降价递增 `ranking_epoch`；收盘按最终价以最低价基准法（价格分 = 最低有效价 / 己方最终价 × 100）重算加权总分，评审轮次的非价格分沿用。
- **冻结快照**：收盘生成成交排名快照并记录内容哈希；授标前校验哈希，篡改即拒。快照在显式收盘、到点后台清扫（每 0.5 秒）、或服务重启恢复时幂等生成。
- **可恢复**：竞价行、报价、事件（`auction_events`）与状态在同一 SQLite 事务落库，结束时间以库内 `ends_at` 为准；进程重启后 `recover()` 继续计时，到点未收盘的自动补收盘（快照标记 `recovered: true`）。
- **供应商视图**：可见当前最低价、自己的报价和公开的降价事件（价格/加时），其他入册供应商身份隐藏。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整开标授标、截止前正文隐藏、利益冲突、重复评分覆盖、投诉重评和角色权限；反向竞价覆盖准入冻结、版本校验、并发后到者保留输入、最后两分钟顺延与令牌去重、排名重算、授标门禁、快照防篡改和崩溃恢复。

## 局限

供应商与请求用户没有绑定校验，身份仍依赖请求头；投标正文虽然按接口阶段隐藏，但数据库本身未加密；评分规则适合演示，不覆盖复杂资格预审、保证金、电子签名和采购法规差异。
