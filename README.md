# paxel-local

把两份互补的工作合成一条可运行的链路：**本地分析管线（L1–L4）+ Paxel 的真实线上协议（L5）**。

不需要 YC 账号，不需要那个 911MB 的 amd64 镜像，也不向任何第三方发送业务数据——
除了你自己显式指定的模型端点。

```
transcript/git ──► pipeline/（证据 → 叙事输入 → 提交分组 → 片段）
                        │
                        ▼
   runner.py：会话叙事（/v1/messages，逐段 + 合并）
                        │
                        ▼
              runner.py：片段打分（叙事作为上下文）
                        │
                        ▼
   transport.py：scoring_sessions → manifest → results(gzip) → 后端
```

## 两部分各来自哪

| 模块 | 来源 | 覆盖层 |
|---|---|---|
| `pipeline/` | 之前的 Python 重写：会话收集、事件归一化、叙事分段、PR/2 小时提交分组、片段关联（带置信度）、五轴评分 | paxel L0–L4 |
| `transport.py` | 从真实客户端 `0.3.83.2` 抓包还原的协议：7 个端点、gzip 结果、幂等键、nonce 提取 | paxel L5 |
| `runner.py` | 两边粘合：用管线的片段输入调 `/v1/messages`，把结果组装成 15 键 payload 上传 | L4/L5 接缝 |
| `git_enrich.py` | 只用本地 git：作者过滤、分支匹配（`git branch --contains`）、diff 摘要、评审信号 | L3 补强 |
| `mock_server.mjs` | 本地假服务端：按实测契约应答，并把每个请求写到 NDJSON | 无（仅测试用） |
| `contracts/` | 实测契约与抓包解码结果 | 证据 |
| `prompts/episode_scoring.txt` | 原版 v15 评分提示词（从旧镜像提取，非最新 v16） | L4 |
| `prompts/session_narrative*.txt` | **本仓库自行编写**的叙事/合并提示词，只沿用五节结构，不是 Paxel 原文 | L2 |

## 用 npm 一条命令跑

```bash
npx github:<你的账号>/ai-session-audit /你的/项目目录 --days 90
# 或
npm i -g ai-session-audit && ai-session-audit /你的/项目目录
```

前置只有两样：**Node 18+** 和 **Python 3.9+**。

不带 `--proxy/--results` 时，CLI 会自动起一个本地 mock 后端（`127.0.0.1:8899`），
整条链路不出本机；要接自己的服务就显式传：

```bash
ai-session-audit /项目 --proxy http://你的后端 --results http://你的后端/api/v1/results --token xxx
```

## 报告

每次跑完会写 `report.html`（评分完成后生成，含片段标题、五轴分数、叙事、git 证据）；
管线自己那份报表会保留为 `pipeline-report.html`。不重新跑也能刷新：

```bash
npx ai-session-audit /项目 --out 输出目录 --report-only
```

## 用真模型跑（可选，分数才不是 mock）

本地代理支持把 `/v1/messages` 转发给任意 OpenAI 兼容端点，默认 GLM：

```bash
export GLM_API_KEY=...            # 只在 shell 里，别写进仓库
export GLM_MODEL=glm-4.5          # 可选，默认取 --model
npx ai-session-audit /项目 --limit 1 --model glm-4.5
```

要点：

- 不设 `GLM_API_KEY` 时走内置固定回答，只验证链路；设了之后叙事和评分都是真模型输出。
- 推理模型（glm-4.5）会把 token 先花在 reasoning 上，代理里已把 `max_tokens` 下限抬到 4096，否则会返回空文本。
- 默认只给「即将评分的片段」所涉及的会话生成叙事；`--all-narratives` 才会跑全部。

## 快速开始（完全本地）

```bash
# 1) 起本地假服务端
node mock-server.mjs &

# 2) 分析一个项目并走完整协议
python3 runner.py /你的/项目目录 --days 90 --limit 5 \
  [--git-repo /仓库路径] [--author-email you@example.com] [--no-enrich-git]

# 结果：paxel-local-out/{evidence,episodes,episode_inputs,results-payload,run-summary}.json
```

`run-demo.sh` 把这两步串起来并自动收尾。

## 接到你自己的后端

`mock_server.mjs` 的七个路由就是服务端的实现清单。换掉地址即可：

```bash
python3 runner.py /项目 \
  --proxy http://127.0.0.1:9000 \
  --results http://127.0.0.1:9000/api/v1/results \
  --token "$YOUR_TOKEN" \
  --model deepseek-chat
```

命令行会依次打印：health → validate → preflight（prompt id 契约）→ scoring session →
manifest 提交 → 逐片段打分 → gzip 上传。任何一步失败都会带 HTTP 状态和响应片段。

## 现在真实覆盖到哪

| 层 | 状态 |
|---|---|
| L0 采集（Claude Code / Codex） | ✅ 管线的 Python 实现 |
| L1 事件归一化 | ✅ |
| L2 叙事（多段 → 合并 → 进片段上下文） | ✅ 走 `/v1/messages`，与评分共用同一 nonce 体系 |
| L3 提交分组 / 片段关联 | ✅ 新增 `git_enrich.py`：作者过滤、分支匹配、diff 摘要、评审信号 |
| L4 五轴评分 | ✅ 走 `/v1/messages`，提示词为 v15 |
| L5 协议（7 端点、gzip、幂等、nonce） | ✅ 与抓包逐字段一致 |
| L6 服务端（聚合、报告、信号） | ❌ 只有 mock；真实实现不在本仓库 |
| L7 评委质量评估 | 见 `../judge-eval/`（扰动、跨度），未接入本仓库 |

## 明确不做的

- **不连 YC**：默认端点全是 `127.0.0.1`。要连真实服务需自己改地址并承担 ToS 与数据风险。
- **不包含逆向源码**：`paxel-audit/` 里的 Rails 提取物不在本仓库，也不应随仓库分发。
- **不声称同分**：提示词是 v15 移植版，模型也不是 `gpt-6-sol-none`，分数不能当作 Paxel 官方分数。

## 已知缺口

1. 叙事（L2）在协议模式下没有实际生成——需要接一个模型调用，或让管线先跑 `--score`。
2. 片段路径匹配目前依赖 cwd + 时间窗，置信度多为 0.3–0.5，不能当作“提交归属已证明”。
3. `session_events` 直接进了 payload，样本大时 payload 会到几百 KB（gzip 后 ~70 KB）；上传前应做截断。
4. 服务端（L6）没有：`slug` 生成、幂等存储、nonce 对账、报告聚合都需要自己实现。

## 目录

```
paxel-local/
├── runner.py                 # 编排：pipeline → 打分 → 上传
├── transport.py              # 协议客户端（7 端点）
├── git_enrich.py             # 本地 git 证据补强
├── mock_server.mjs           # 本地假服务端（同时是服务端实现清单）
├── run-demo.sh
├── pipeline/                 # 分析管线（L0–L3）
│   ├── paxel_reproduction.py
│   ├── paxel_score.py
│   └── project_analyzer.py   # 另一条产品线：项目级准入前分析，不属 Paxel
├── prompts/episode_scoring.txt
└── contracts/                # 实测契约 + 抓包解码
```
