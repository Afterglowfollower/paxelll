# Paxel 客户端 ↔ 服务端契约（静态提取）

> 来源：`codesoda/paxel` 提取出的 Rails 客户端源码（`paxel-audit/rootfs/rails`），逐行核对，未抓包。
>
> ⚠️ **本文描述的是 0.3.44.6 时代的契约。之后用真实镜像（0.3.83.2）+ 本地 mock 实测发现契约已演进**：
> preflight 改用 prompt id、新增 `scoring_sessions` 与 `manifest` 两个端点、`/v1/messages` 不再发 `system`
> 而改由服务端按 `x-paxel-prompt-id` 注入、results 改成 gzip。实测版本见
> [paxel-capture/paxel-live-contracts.md](paxel-capture/paxel-live-contracts.md)，以那份为准，本文保留作演进对照。

> 结论先放这：**三条契约的请求/响应结构在客户端源码里是完整的**，抓 HAR 只能再确认服务端生成的取值（contract_version 的十六进制、nonce 的 HMAC 值），而这些在我们自建后端里本来就由自己定义。所以这份文档可以直接当后端实现的接口规格。

## 0. 端点拓扑

| 逻辑端点 | 实际地址 | 谁配置的 |
|---|---|---|
| 授权（设备码） | `https://paxel.ycombinator.com/upload` | `upload.sh:154`、`177` |
| LLM 代理 | `https://paxel-llm.ycombinator.com` | `upload.sh:178`；容器内 `YC_LLM_PROXY_URL` |
| 结果上传 | `https://paxel.ycombinator.com/api/v1/results` | `upload.sh:5232`（`docker_server=$PAXEL_SERVER`）+ `5584`；`PAXEL_SERVER` 默认值见 `upload.sh:177` |

容器注入（`upload.sh:5578-5584`）：

```
-e YC_TOKEN=<token>  -e YC_API_KEY=<token>  → 同一个 token，分别用于 results 和 proxy
-e YC_LLM_PROXY_URL=<proxy>  -e YC_RESULTS_ENDPOINT=<results>
```

注意：`YC_API_KEY` 被塞的是 YC 代理 token，不是 Anthropic key。`anthropic_client.rb:891-907` 明确 fail-closed——没有 proxy base_url 且带 YC token 时直接抛错，防止把 token 发给 api.anthropic.com。

---

## 1. `GET {proxy}/health`

`client_pipeline.rb:648-714`

- 无 body、无自定义 header（纯 GET）
- 超时：open 5s / read 5s
- 成功：2xx + `{"signatures_loaded": true}`
- 503 + `{"status":"draining"}` = 发布中，退避重试（2s、4s，共 3 次）
- 其它非 2xx 立即致命；`signatures_loaded` 为假也致命（"proxy is running but has no signatures loaded"）
- 网络错误（ECONNREFUSED/RESET/TIMEOUT/SocketError）同样 3 次退避重试

## 2. `POST {proxy}/validate`

`client_pipeline.rb:716-760`

- Headers：`Content-Type: application/json`、`x-api-key: <token>`、`x-paxel-client-release: <VERSION>`
- Body：空
- 成功：2xx 即通过
- 失败：非 2xx，响应体 `{"reason": "..."}` 映射到用户文案：

| reason | 含义 |
|---|---|
| `expired` | token 过期 |
| `revoked` | token 吊销 |
| `daily_cost_exceeded` | 当日成本超限 |
| `daily_calls_exceeded` | 当日调用数超限 |
| `exhausted` | token 总用量用尽 |
| `proxy_exhausted` | 代理调用数用尽 |
| `not_found` | token 不认识 |

## 3. `POST {proxy}/api/v1/proxy/preflight`

`anthropic_client.rb:46-170`

- URL 拼接：`URI.join(base_url, "api/v1/proxy/preflight")`，base_url 就是 `YC_LLM_PROXY_URL`
- Headers：`Content-Type: application/json`、`X-YC-Token: <token>`、`x-paxel-client-release: <VERSION>`
- 超时：open 5s / read 10s
- Request body：

```json
{
  "client_signatures": ["<16 hex>", "..."],
  "client_version": "0.3.44.6"
}
```

**签名怎么算**（`proxy_call_validator.rb:110-184`）：

- 单条签名 = `SHA256(完整 system prompt 文本)[0..15]`（16 位十六进制，前缀截断，不是前缀哈希——hash 的是全文）
- 客户端自己跑同一套 prompt 构建代码，把当前签名集合发上去
- 当前集合 = 4 条：
  1. `SessionNarrativeAnalyzer::SYSTEM_PROMPT`
  2. `SessionNarrativeAnalyzer::MERGE_SYSTEM_PROMPT`（>60K token 多段叙事用）
  3. `V3ScoringPromptBuilder.build_episode_prompt(nil)`（episode 打分）
  4. `DecisionClassifier.system_prompt_for_validation`（含 law catalog 的动态 prompt）
- `contract_version` = `SHA256(sorted(signatures).join("\n"))[0..15]`

**响应分支**：

| HTTP | 响应体 | 客户端行为 |
|---|---|---|
| 200 | `{"contract_version": "...", "warning": "legacy"?, "bun_signatures_stale"?: bool, "bun_signatures_age_ms"?, "bun_signatures_refresh_interval_ms"?}` | `warning=="legacy"` → 提示重建；否则 ok |
| 403 | `{"code": "client_out_of_date", "expected_contract_version": "...", "client_contract_version": "...", "missing_signatures": [...]}` | 抛 `PaxelLlmError::ClientOutOfDate`，终止本轮 |
| 403（无 code） | 任意 | 跳过，交给 `/validate` 暴露鉴权错误 |
| 404 | — | 跳过（老服务端没有该路由，fail-open） |
| 5xx | — | 跳过 |
| 503 | `Retry-After` | 最多 3 次，等待 `clamp(Retry-After,1,5)` 秒 |

服务端侧的校验规则（`proxy_call_validator.rb:126-155`）：model 必须在白名单 → `system` 文本不能空（支持 content-block 数组，取 text 拼接）→ 签名命中 current 或 legacy 才放行，否则 403 `Unknown call signature`。legacy 集合由硬编码 8 条 + `YC_PROXY_LEGACY_SIGNATURES` 环境变量补充，用于 prompt 轮换后的过渡窗口（约 14 天）。

**模型白名单**（`proxy_call_validator.rb:41-53`）：

```
claude-haiku-4-5-20251001 / claude-haiku-4-5
claude-sonnet-4-20250514 / claude-sonnet-4-6-20250603 / claude-sonnet-4-6
gpt-5.5{-none,-low,-medium,-high,-xhigh}
gpt-5.4-mini{-none,-low,-medium,-high,-xhigh}
```

## 4. `POST {proxy}/v1/messages`

Anthropic Ruby SDK 直连（`anthropic_client.rb:886-921`）：

- `base_url = YC_LLM_PROXY_URL`，`api_key = YC_API_KEY`（= YC token）
- SDK 自带：`x-api-key`、`anthropic-version`
- 额外 header：`x-paxel-client-release`（所有请求）、`x-paxel-session-id`（在有会话上下文时，`anthropic_client.rb:239-243`）
- Body：标准 Anthropic Messages API（`model`、`max_tokens`、`system`（可能被 cacheable_system 转成 content-block 数组以吃 prompt caching）、`messages`、`temperature` 等）
- 客户端重试：`MAX_LLM_RETRIES = 5`，`529 overloaded` 用 `OVERLOAD_MAX_RETRIES = 10`；单次硬超时 300s；尊重 `Retry-After`；30s 以上打慢调用告警

**响应里夹带的验证 nonce**（`client_pipeline.rb:96`、`llm_result_cache.rb:317`）：

```
<!-- yc_verify:<request_id>:<hmac> -->
```

服务端在 LLM 输出里注入这一行 HTML 注释；客户端用正则捞出来，`request_id` + `hmac` 一起进上传体的 `nonces`。这是防"伪造打分"的核心：分数必须能对上服务端 `proxy_log` 里真实发生过的调用。

## 5. `POST {results_endpoint}`

`client_pipeline.rb:1883-1960`、`1471-1596`

> 端点没有硬编码默认值：`client_pipeline.rb:124` 只取构造参数或 `ENV["YC_RESULTS_ENDPOINT"]`，两者都为空时整段上传直接跳过（`:545`）。源码注释里出现的 `paxel.yc.com`（`:21`）只是旧示例，实际地址来自 `upload.sh` 注入的 `${PAXEL_SERVER}/api/v1/results`。

- Headers：`X-YC-Token`、`X-Idempotency-Key`（每次 upload 生成的 UUID）、`Content-Type: application/json`
- 超时：60s（`f.options.timeout = 60`）
- 上传前：整个 payload 过 `NullByteSanitizer`（Postgres 不收 U+0000）
- 成功：200 或 201，响应体

```json
{ "slug": "...", "results_url": "...", "idempotent_replay": true|false }
```

`idempotent_replay` 表示这个 `X-Idempotency-Key` 之前提交过，服务端返回的是重放结果。

**请求体 13 个顶层键**：

| 键 | 内容 |
|---|---|
| `episode_scores[]` | `episode_id, request_id, title, type, scores, confidence, session_ids[{session_id, link_type, link_confidence}]` |
| `session_summaries{}` | `session_id → narrative`（截断 2000 字符，已脱敏） |
| `session_metadata{}` | `message_count, first_prompt(≤200), session_created_at, session_modified_at, is_subagent, parent_session_id, session_signals, session_events, dispatch_metadata, quality_flags, active_time_windows, user_highlights(≤10K), steering_trace, agent_type, session_intent, cross_tool_origin, triggered_by_session_id, triggered_by_confidence`（`.compact`） |
| `git_metrics{}` | 只保留 `velocity, numstat, total_commits, total_repo_commits, loc_stats` |
| `git_remote` | 多仓库聚合时为 null |
| `recent_commits[]` | 多仓库聚合时为空；`subject` 已脱敏，author/email 保留 |
| `session_count` / `subagent_session_count` | 整数 |
| `project_name` | 聚合时 `"All projects (N repos)"` |
| `nonces[]` | `"request_id:hmac"` 去重 |
| `decisions[]` | `session_id, proposal_text, response_text, proposal_type, decision_type, law_key, classification_confidence, decision_narrative, option_count, response_word_count, domain, significance, reversibility, event_index, agent_recognized, outcome{signal,confidence,evidence}` |
| `decisions_status` | `dig_status` 或 `"not_run"` |
| `code_quality{}` | `status` + 结构化结果，`error` 文本已脱敏 |
| `client_telemetry{}` | 见 `package_telemetry` |

`session_events` 的文本按 `UPLOAD_EVENT_TEXT_LIMIT = 2000` 截断后才上传；`user_highlights` 按 `UPLOAD_USER_HIGHLIGHTS_LIMIT = 10000`。

**失败与重试**：

- `UPLOAD_MAX_ATTEMPTS = 3`
- 4xx（429 除外）**不重试**，直接失败并上报 Sentry
- 429 / 5xx 退避重试
- 3 次都失败的 payload 落盘 stash（`pending_upload_stash.rb`），下次运行由 `pending_upload_replay_service.rb` 补传；补传要求 endpoint 与当前 `YC_RESULTS_ENDPOINT` **一致**，否则跳过（`pending_upload_replay_service.rb:10,73`）

---

## 6. 自己复现后端时，哪些必须一致、哪些可以自定义

**必须一致**（客户端写死了，不一致就跑不通）：

- 五个路径与方法：`GET /health`、`POST /validate`、`POST /api/v1/proxy/preflight`、`POST /v1/messages`、`POST {results}`
- 鉴权 header 名：`X-YC-Token`（results / preflight）、`x-api-key`（messages / validate）
- 关键响应字段名：`signatures_loaded`、`contract_version`、`code=client_out_of_date`、`expected_contract_version`、`missing_signatures`、`slug`、`results_url`、`idempotent_replay`、`reason`
- 幂等语义：同 `X-Idempotency-Key` 必须返回同一结果并标 `idempotent_replay`

**可以自定义**（客户端不做校验，只透传）：

- `contract_version` 的具体算法（客户端只做字符串比较/回显）
- nonce 的 `request_id` 形态与 HMAC 密钥（客户端只负责把命中正则的那串原样回传）
- 结果入库后的聚合、评分、报表

**必须自己补的**：`/health` 的 `signatures_loaded` 语义（re-fetch 签名的就绪标志）、`/validate` 的配额/吊销模型、`proxy_log`（按 `request_id` 存调用记录，供 nonce 校验）、`episode_scores[].request_id` 与 proxy_log 的匹配逻辑（服务端 `results_controller.rb` 的 ResultVerifier 行为在源码里只剩注释线索：按 `request_id` 匹配，null 时退回按数组下标匹配）。

---

## 7. 为什么没有直接抓 HAR

跑一次真实客户端需要同时满足三件事，其中两件不该由我替你决定：

1. **YC 账号**：`upload.sh` 走浏览器设备码授权，产物是 `~/.paxel/token`（本机现在没有这个文件）。
2. **会把你的 transcript 传出去**：这次运行会把本机 Claude Code / Codex / Cursor 的会话内容（脱敏后）上传到 YC。这是真实的外部写操作，需要你明确同意。
3. **抓 TLS 要装 CA**：`paxel-llm.ycombinator.com` 和 `paxel.yc.com` 都是 HTTPS，mitmproxy 的 CA 必须注入容器信任链，否则只能拿到 CONNECT 隧道名。

如果仍然想做一次实测，顺序是：

```bash
# 1) 装 mitmproxy（独立 venv，避免污染系统 Python）
uv venv ~/.local/share/mitm && uv pip install --python ~/.local/share/mitm/bin/python mitmproxy

# 2) 起代理
~/.local/share/mitm/bin/mitmdump -p 8080 --set confdir=~/.mitmproxy

# 3) 拿 CA 并让容器信任（把证书挂进 /usr/local/share/ca-certificates 后 update-ca-certificates，
#    或用 HTTPS_PROXY + SSL_CERT_FILE 指向挂载的 mitmproxy-ca-cert.pem）

# 4) 用 upload.sh 的 docker run 参数跑，额外注入
#    -e HTTPS_PROXY=http://host.docker.internal:8080 -e SSL_CERT_FILE=/certs/mitm.pem
#    -v ~/.mitmproxy/mitmproxy-ca-cert.pem:/certs/mitm.pem:ro

# 5) 导出 HAR：mitmdump 的 save.har 插件，或 mitmweb 的 File → Save → HAR
```

拿到 HAR 后我能做的是：用 `inspect_web_network_capture` 逐条核对上表的字段是否与服务端实际返回一致，特别是 preflight 的 `contract_version` 取值和 results 的真实响应体。**但请注意**：这一步会把一次真实上传的请求体（含 transcript 派生的分析结果）落在 HAR 文件里，属于敏感数据，建议只在本地、用完即删。
