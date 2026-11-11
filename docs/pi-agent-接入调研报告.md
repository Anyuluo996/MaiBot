# MaiBot 接入 pi agent 调研报告

> 日期：2026-09-29
> 目标：给 MaiBot（wo4 部署）接入通用 agent 能力，后端选型 pi agent；调研相关项目与 MaiBot 插件生态，论证是否集成到沙盒，给出实施方案。

---

## 1. 结论摘要（TL;DR）

1. **pi 适合做 MaiBot 的 agent 后端**。它是 MIT 协议的极简 agent harness（earendil-works/pi，110k stars），原生提供三档无头接口（print/JSON、RPC、SDK），Python 侧零依赖即可驱动；`models.json` 原生支持 OpenAI 兼容自定义 provider，可直接对接 wo4 的 new-api 网关，不绑定任何厂商。
2. **必须隔离，不能裸跑在 bot 容器里**。pi 官方明确"没有内置权限系统，默认以启动用户的全权限运行"，README 直接建议容器化。wo4 的 maim-bot-core 容器内有 bot 配置、new-api 密钥、群管插件数据，让群消息驱动的 agent 在其中自由执行 bash 是不可接受的攻击面。
3. **推荐方案：把 pi 装进现有 aio-sandbox 容器，插件通过 maimai_sandbox 已验证的 REST 通道（`/v1/shell/exec`）驱动，one-shot `pi -p` + 提交-轮询模式**。aio-sandbox 本来就是按"不可信执行环境"设计的边界，pi 的文件/bash 操作天然被容器吸收；bot 侧密钥不进沙盒，pi 只持有可吊销、限额的 new-api 子 token。演进方向（P3）是独立 pi 容器 + RPC 常驻会话。
4. **生态已有成熟先例可直接抄**：官方插件市场的 `maibot-codex-plugin`、`maiclaude` 插件已经把"QQ 命令 → CLI 编码代理 → 进度/产物回传"整条链路（含 SnowLuma file 段回传，正是 wo4 的适配器）踩通了；本地 `maimai_sandbox` 的 REST 客户端、权限门、提交-轮询模式可整体复用。
5. **两个必须先解决的硬风险**：① aio-sandbox 容器的 Node 版本必须 ≥ 22.19（pi 硬性要求），需 P0 验证；② wo4 的 aio-sandbox 每天 05:13 无条件重置 cron，会杀掉长任务并清空 workspace/session，需挂持久卷或改部署形态。

---

## 2. pi agent 项目本体

### 2.1 基本信息

| 项 | 值 |
| --- | --- |
| 仓库 | https://github.com/earendil-works/pi（原 badlogic/pi-mono，已迁组织） |
| 官网 | https://pi.dev |
| 许可 | MIT |
| npm 包 | `@earendil-works/pi-coding-agent`（当前 0.87.0，~23MB 解包） |
| 运行要求 | **Node.js ≥ 22.19.0**（engines 硬约束） |
| 维护者 | Mario Zechner (badlogic)、Armin Ronacher (mitsuhuer) 等 |

包结构：`pi-coding-agent`（CLI）、`pi-agent-core`（agent 运行时/工具调用）、`pi-ai`（统一多 provider LLM API）、`pi-tui`、`pi-durable`（持久会话）、`chord`（应用编排）。

### 2.2 四种运行模式（对集成最关键）

| 模式 | 用法 | 适用 |
| --- | --- | --- |
| Interactive | `pi`（TUI） | 人工使用，bot 场景不用 |
| **Print/JSON** | `pi -p "query"`，`--mode json` 输出事件流 | 一次性任务、脚本化调用，**MVP 首选** |
| **RPC** | `pi --mode rpc`，stdin/stdout 上的 JSONL 协议 | 长驻会话、双向交互（官方文档自带 Python 客户端示例），**P3 演进** |
| SDK | Node 内嵌 `AgentSession` | 仅适合 Node 宿主，Python 侧不适用 |

Print 模式细节：stdin 可管道输入任务文本；默认不落 session，`--session <path>` 指定会话文件、`--continue` 续最近会话（CHANGELOG 确认 `--session` 在 print 模式下的保存 bug 已修复）；`--provider`/`--model` 运行时选模型。

RPC 模式能力（docs/rpc.md）：`prompt`/`steer`/`follow_up`/`abort` 命令，`get_state`/`get_messages`/`set_model`/`compact`/`switch_session`/`export_html` 等 30+ 命令；事件流含 `agent_start/agent_settled`、`message_update`（流式 delta）、`tool_execution_*`（工具进度）等。协议是严格 JSONL（仅 `\n` 分隔）。

### 2.3 能力与扩展体系

- **会话树**：所有分支存单个文件，可回溯任意节点续跑；`/export` 导出 HTML。
- **自动 compaction**：接近上下文上限自动摘要压缩，可用扩展自定义。
- **扩展（TypeScript 模块）**：可注册工具、命令、事件钩子；sub-agent、权限门、沙盒、MCP 集成等都以扩展/包形式存在（官方 50+ 示例包，`pi install npm:…`/`git:…` 安装）。
- **Skills**：SKILL.md 格式能力包，按需加载（渐进披露，不爆 prompt cache）。
- **Provider**：内置 Anthropic/OpenAI/Google/Bedrock/OpenRouter/MiniMax/Kimi/Qwen 等 30+；**自定义 provider 用 `~/.pi/agent/models.json`**（`baseUrl` + `api`（如 `openai-completions`）+ `apiKey` + `models` 数组），auth.json 支持用 `!command` 从密钥管理器取 key 不落盘。
- **官方参考集成 pi-chat**（https://github.com/earendil-works/pi-chat）：把 Discord/Telegram 频道桥接到沙盒内的 pi 会话——每频道一个 Gondolin 微虚拟机 + tmux worker，持久 workspace、memory.md、skills，回复触发、流式编辑、聊天内远程控制（stop/status/compact/new）。这是"聊天工具 ↔ 沙盒 pi"的官方架构蓝本，与本次需求同构（只是协议侧换成 QQ/MaiBot）。

### 2.4 安全定位（决定集成形态的核心事实）

README 原文："**Pi does not include a built-in permission system** for restricting filesystem, process, network, or credential access. By default, it runs with the permissions of the user and process that launched it."

官方 `containerization.md` 给出三种隔离模式：**Gondolin**（宿主 pi + 工具路由进 QEMU 微虚拟机，需 Node ≥ 23.6 + QEMU）、**Plain Docker**（整个 pi 进程进容器）、**OpenShell**（策略网关沙盒）。→ 对我们的结论：pi 的执行面必须放进一个容器边界内，这与"是否集成到沙盒"的回答直接对应（见第 5 节）。

---

## 3. 相关项目与 MaiBot 插件生态调研

### 3.1 MaiBot 官方插件市场（plugin-repo plugins.json）中的直接先例

| 插件 | 形态 | 与本需求的关联 |
| --- | --- | --- |
| **arcczr.remote-codex-agent**（maibot-codex-plugin） | QQ `/codex` 命令 → 本机 Codex CLI 子进程 | **最完整同构先例**。task/session/resume 三级任务模型、`codex exec --json -s workspace-write --output-last-message final.md` + stdin 传 prompt、进度节流转发（最小间隔 5s、条目合并）、`artifact_globs` 扫描产物、NapCat `upload_group_file` / **SnowLuma OneBot file 段**双通道回传、`qq:` 前缀用户白名单、remote 模式 HTTP `/v1/tasks` 提交-轮询（5s 间隔）。 |
| **qr0w.maiclaude**（maibot-maiclaude-plugin） | 同上，后端 Claude Code CLI | 验证了 `--dangerously-skip-permissions` 无人值守模式的**风险与缓解**：cwd 限定、env 白名单透传、SSRF 校验、`--max-turns`、`--max-budget-usd`；README 明确警告子进程拥有宿主完整文件系统权限。 |
| **sansenjian.quick-e2b-sandbox** | LLM Tool → E2B 云沙盒执行 Python | "Tool + 外部沙盒 + 提交-轮询"模式的轻量先例；E2B 是海外云服务，国内需代理，不如本地 aio-sandbox。 |
| CharTyr.MaiBot_MCPBridgePlugin | MCP 桥 | 备选路径：若 pi 侧配 MCP 即可让 MaiBot 直接调用，但功能面窄（无 workspace/产物概念），不满足"完整 agent"需求。 |
| pulsarmatrix.servercmd / xuesheng.maibot-server-command | 服务器命令执行 | 说明社区对"bot 操作服务器"有需求，但均无 agent 循环。 |

**结论：社区无 pi 接入先例；codex/maiclaude 两插件的交互 UX（命令、任务模型、产物回传、SnowLuma 适配）已被验证，可直接借鉴成新插件的骨架。**

### 3.2 本地插件体系事实（本次代码调研确认）

- 插件体系为 Host/Runner 子进程 + msgpack RPC，SDK `maibot-plugin-sdk` 2.8.0；`@Tool` 装饰器注册 LLM 工具，`execute` 收 dict kwargs（含 `stream_id`/`message`），返回 dict/str；图片可用 `content_items`（base64）随返回值拆分发送（`docs/插件开发指南.md`）。
- `ctx.send` 有 text/image/emoji/hybrid/forward/command/custom，**无 send.file**——文件回传走 `send.custom` 的 OneBot `file` 段（codex 插件已验证 SnowLuma 路径）。
- `ctx.llm.generate` 可在插件内调 LLM（本集成中 pi 自带 LLM，无需）。
- **工具 RPC 默认超时 60s**（`src/plugin_runtime/host/component_timeout.py:6`）→ 长任务必须"提交-轮询"两工具模式，`maimai_sandbox` 的 CF 链接扫描已内置同款先例（60s 未完成返回 `scan_id`，由 `query_url_scan` 续查）。
- `maimai_sandbox` v1.4.1（`plugins/maimai_sandbox/`）已有的可复用资产：
  - `sandbox_client.py`：httpx AsyncClient + `X-AIO-API-Key` 鉴权 + `/v1/shell/exec`（body 带 timeout/hard_timeout）+ 统一 `{success,message,data}` 信封 + 错误统一抛 `SandboxClientError` 不兜底；
  - OBSERVE Hook 缓存 stream→QQ 号的发起者识别；`_sb_allowed` 权限门（tiered/strict）；
  - `tests/`：`httpx.MockTransport` 契约测试 + 轻量 FakeHost 权限测试，可直接照抄为 pi 插件测试。

### 3.3 aio-sandbox（执行侧底座）

- 上游：**agent-infra/sandbox**（字节跳动 agent-infra，https://github.com/agent-infra/sandbox），单容器集成 Browser/Shell/File/MCP/VSCode Server；服务本体为 Node.js，wo4 现运行 1.11.0。
- wo4 现状（记忆档案）：作为 maimai_sandbox 的不可信执行容器；root 级 browser-supervisor 持有 Chromium；**每 30 分钟健康探测 + 每天 05:13 无条件重置**（cron 自愈）。

---

## 4. 方案对比

| | A. 裸跑宿主容器（codex/maiclaude 模式） | **B. 装进 aio-sandbox（推荐 MVP）** | C. 独立 pi 容器（推荐演进） |
| --- | --- | --- | --- |
| 执行位置 | maim-bot-core 容器内 spawn 子进程 | aio-sandbox 容器内 `/v1/shell/exec` | 专用容器（node:24 + pi，官方 Plain Docker 模式） |
| 隔离性 | ✗ pi 全权限触及 bot 配置/密钥/插件数据；watchtower 自动更新官方镜像，装进去的 Node/pi 不持久 | ✓ 复用已按"不可信"加固的边界；bot 密钥不进沙盒 | ✓ 最干净，资源可独立限额 |
| 驱动复杂度 | 低（subprocess） | 低（复用 sandbox_client REST） | 中（需 docker exec / 自建 HTTP 包装或 SSH） |
| 长任务/会话 | 进程随 bot 容器 | 受每日 05:13 重置影响（需持久卷） | 容器独立生命周期，重置策略自定 |
| 新增运维 | 无 | 无（若 Node 版本满足） | 一个常驻服务 |
| E2B 云沙盒 | — | — | ✗ 海外依赖 + 代理，数据出境，不取 |

**否决 A 的决定性理由**：pi 无权限系统（§2.4）+ 群消息即 prompt 的注入面 + maim-bot-core 内有真实密钥（new-api、群管、TTS 等）；且官方镜像被 watchtower 滚动更新，任何容器内手工安装（Node、pi）都会在下一次更新丢失。maiclaude 插件作者自己也只能建议"用专用低权限账户运行"来缓解——在 wo4 上这条建议的落地形态恰好就是"挪进沙盒容器"。

**选 B 做 MVP 的理由**：零新增基础设施；`/v1/shell/exec` 通道、鉴权、超时、错误信封、权限门、测试范式全部现成；aio-sandbox 本来就跑任意用户代码，pi 的风险不超过现状。**C 作为演进**：当需要 RPC 长驻会话（流式进度、steer 中途干预）、独立资源限额、或 aio-sandbox 的 Node 版本不达标时切换。

---

## 5. 推荐架构（方案 B）

### 5.1 数据流

```
QQ 群/私聊消息（SnowLuma 适配器）
   │  /pi <任务描述>            或 LLM 自主调用 Tool pi_agent_run
   ▼
MaiBot 宿主（maim-bot-core）
   └─ 插件 maimai_pi_agent（Runner 子进程，Python）
        ├─ Command: /pi、/pi status、/pi session、/pi continue、/pi cancel
        ├─ Tool(@Tool visible): pi_agent_run → 立即返回 task_id（提交-轮询，规避 60s RPC）
        ├─ Tool: pi_agent_query(task_id) → 状态/摘要/产物列表
        └─ 后台 asyncio 任务：轮询沙盒内任务文件，完成后 ctx.send 回传
   ▼  REST（httpx，复用 sandbox_client 模式，X-AIO-API-Key）
aio-sandbox 容器（不可信边界）
   ├─ /v1/shell/exec: nohup pi -p --mode json --session <sid> \
   │      -C /data/pi/<task_id>/workspace < prompt.md > run.jsonl 2> err.log &
   ├─ workspace: /data/pi/<task_id>/{input, artifacts}
   └─ ~/.pi/agent/models.json → new-api（api: openai-completions，独立子 token）
   ▼
new-api 网关（OpenAI 兼容）→ 上游模型（走 wo4 现有模型池）
```

### 5.2 关键设计点

1. **任务执行**：每次 `/pi` 通过 `/v1/shell/exec` 发一条 `nohup pi … &` 立即返回；插件侧每 5–10s 轮询 `run.jsonl` 尾部与 `final.md`（`--output-last-message` 等价物：print 模式 `--mode json` 的最后一条 assistant 消息 / `pi -p` 纯文本输出重定向）。完全复刻 codex 插件在 remote 模式验证过的提交-轮询时序。
2. **会话续接**：QQ 用户 ↔ pi session 文件一一映射（`/data/pi/sessions/<qq>.jsonl`），`/pi continue` 走 `pi -p --continue`；跨容器重启的会话恢复靠 `--session <path>`。pi 的 compaction 自动管理上下文长度。
3. **Provider 配置**：容器内 `~/.pi/agent/models.json` 定义指向 new-api 的 provider（`baseUrl: http://<new-api>/v1`，`api: openai-completions`），模型显式列出并锁定版本；密钥优先经 `/v1/shell/exec` 的 env 注入（若端点不支持 env，则落 `auth.json` 并 chmod 600——**P0 验证项**）。token 用 new-api 独立子令牌：只放行指定模型、设额度上限、可随时吊销。
4. **产物回传**：任务完成扫描 `artifacts/*`（复用 `artifact_globs` 思路）；图片 `ctx.send.image(base64)`，其他文件走 `ctx.send.custom` 的 OneBot `file` 段（SnowLuma 路径，codex 插件已验证），单文件大小上限可配置。
5. **权限与安全门**：整体复用 maimai_sandbox 的模式——OBSERVE Hook 识别发起者、`qq:` 白名单 + tiered gate、高危命令正则（`rm -rf /`、内网探测等）在提交前拦截；pi 的系统提示用 SYSTEM.md 固化为"受约束任务执行器"人设，明确禁止读取容器内凭证文件。
6. **超时与成本**：单任务 `hard_timeout`（建议默认 600s、上限 3600s）；pi 侧设 thinking off（沿用 wo4 关思考的既有结论）；new-api 侧令牌限额兜底；同一 stream/用户并发 1（codex 插件同款限制）。
7. **插件结构**（照 maimai_sandbox 骨架）：

```
plugins/maimai_pi_agent/
├── _manifest.json        # host 1.0.0-1.99.99，capabilities send.text/send.image
├── plugin.py             # MaiBotPlugin 生命周期 + Hook + 后台轮询
├── config_model.py       # sandbox(provider 覆写)/pi(model,thinking,timeout)/
│                         #   security(白名单,gate,高危正则)/task(并发,TTL)/artifact 段
├── pi_client.py          # 仿 sandbox_client：REST + 任务封装 + JSONL 解析
├── commands.py / tools.py
└── tests/                # MockTransport 契约测试 + FakeHost 权限测试
```

### 5.3 P3 演进（独立容器 + RPC）

新建 `pi-agent` 容器（node:24-bookworm-slim + 全局安装 pi + 挂 `/data`），容器常驻一个 `pi --mode rpc` 进程池（每活跃用户一个），由 maim-bot-core 侧的轻量 FastAPI/自写 JSONL 桥暴露为 HTTP。收益：流式进度转发到群、`steer`/`follow_up` 中途干预（`/pi steer 别用 requests 换 httpx`）、`abort` 精确取消、不受 aio-sandbox 每日重置影响。代价：+1 常驻服务与桥接层。官方 RPC 文档自带 Python 客户端样例，桥接层工作量可控（~200 行）。

---

## 6. 风险清单与对策

| # | 风险 | 等级 | 对策 |
| --- | --- | --- | --- |
| 1 | aio-sandbox 1.11.0 容器 Node < 22.19，pi 装不上 | **高（阻断）** | P0 第一项验证 `node -v`；不满足则：a) 容器内并行装新 node（nvm/静态二进制，需持久卷）；b) 升级 aio-sandbox 镜像；c) 直接转方案 C 独立容器 |
| 2 | 每天 05:13 无条件重置杀长任务、清 workspace/session | **高** | `/data/pi`（workspace+sessions+~/.pi）挂宿主 bind mount 持久卷（注意卷目录仍属沙盒可写面，勿与 bot 数据同卷）；或接受重置、任务 TTL 设 24h 内；根治靠 P3 独立容器 |
| 3 | prompt injection：群友诱导 agent 读凭证/打内网 | 高 | 沙盒内不放高价值凭证（pi 只拿可吊销子 token）；SYSTEM.md 约束 + 高危正则门 + 用户白名单；aio-sandbox 出网仅放行 new-api 与白名单域（可在容器网络层限） |
| 4 | 成本失控（agent 循环烧 token） | 中 | new-api 子令牌限额；任务 hard_timeout；模型锁定非推理便宜档 + thinking off；单 stream 并发 1 |
| 5 | 60s 工具 RPC 超时 | 中 | 提交-轮询双工具模式（CF 扫描先例）；命令路径不受此限 |
| 6 | `/v1/shell/exec` 不支持 env 注入 | 中 | 改落 auth.json（600）；或先 `sh -c 'export … && nohup pi …'` 拼装（key 会进 shell 历史/日志需注意）——P0 验证 |
| 7 | SnowLuma file 段兼容性 | 低 | codex 插件已验证该路径；失败时降级为产物列表文本（同款 fallback，但注意项目规范：错误要暴露，不静默兜底——列表+明确错误提示） |
| 8 | pi 迭代快（0.87.0，CHANGELOG 高频） | 低 | 锁定精确版本安装；升级走冒烟脚本（`pi -p "reply OK"` 计时断言） |
| 9 | npm 安装走代理（服务器在国内） | 低 | wo4 已有 17890 代理惯例；npm registry 镜像或代理均可 |

---

## 7. 实施路线图

- **P0 验证（半天，全部在 wo4 aio-sandbox 容器内）**
  1. `node -v`；`npm config get registry`（代理连通性）；
  2. `npm i -g @earendil-works/pi-coding-agent@0.87.0`；`pi --version`；
  3. 写 `models.json` 指向 new-api 子 token，`pi -p "用中文回复：测试成功" --mode json` 冒烟（记录耗时）；
  4. 验证 `/v1/shell/exec` 的 env 注入与 `nohup … &` 后台 + 文件轮询模式；
  5. 验证 `pi -p --session/--continue` 保存与续接；
  6. 确认 05:13 重置对挂载卷的实际影响。
- **P1 MVP（1–2 天）**：插件骨架（manifest/config/tests 仿 maimai_sandbox）；`/pi` 命令 + `pi_agent_run`/`pi_agent_query` 工具；文字摘要回传；权限门与高危正则；本地契约测试（MockTransport）。
- **P2 完善（+1–2 天）**：产物扫描与回传（图片/file 段）；`/pi continue` 会话续接；进度节流转发；任务 TTL 清理；配置模板与版本号（遵守仓库配置规范）。
- **P3 演进（可选）**：独立 pi 容器 + RPC 常驻会话（流式进度、steer 干预、免受每日重置）；按需装 pi skills 扩展包。

---

## 8. 开放问题

1. aio-sandbox 的 `/v1/shell/exec` 是否原生支持 per-call env（决定密钥注入方式）；
2. 挂宿主持久卷后，aio-sandbox 每日重置脚本是否保留挂载内容（预期保留，需实测）；
3. pi 在 print 模式下对长任务的中途取消只能 kill 进程（abort 语义仅 RPC 有）——MVP 接受 kill，P3 换 RPC；
4. 是否给 LLM 自主调用（Tool visible）还是仅命令触发（visible 但白名单 gate=strict）——建议 MVP 先 strict 命令触发，稳定后再放开。

---

## 9. 参考资料

- pi 主仓库/官网/文档：<https://github.com/earendil-works/pi> · <https://pi.dev> · RPC 协议 `packages/coding-agent/docs/rpc.md` · 容器化 `docs/containerization.md` · provider 认证 `docs/providers.md` · 自定义 provider `docs/custom-provider.md` · 选模型 <https://pi.dev/docs/latest/models>
- pi-chat（Discord/Telegram ↔ 沙盒 pi 桥）：<https://github.com/earendil-works/pi-chat>
- AIO Sandbox：<https://github.com/agent-infra/sandbox> · <https://sandbox.agent-infra.com>
- MaiBot 插件先例：<https://github.com/Arcczr/maibot-codex-plugin> · <https://github.com/QR-0W/maibot-maiclaude-plugin> · <https://github.com/sansenjian/quick-e2b-sandbox> · 插件市场索引 <https://github.com/Mai-with-u/plugin-repo>
- 本地：`docs/插件开发指南.md` · `plugins/maimai_sandbox/`（sandbox_client.py / plugin.py / tools.py / config_model.py）· `src/plugin_runtime/host/component_timeout.py`
