# MaiBot 通过 OpenViking 网关接入长期记忆调研报告

> 日期：2026-10-20
> 目标：wo4 上的 OpenViking v0.5.0 网关（10.126.126.2:1935，同日开启）作为 MaiBot 的长期记忆层。本报告梳理网关机制、wo4 部署现状、MaiBot 接入面（代码调研），给出方案对比、推荐架构与实施路线。

---

## 1. 结论摘要（TL;DR）

1. **OpenViking 网关的本质是"带记忆的 LLM API 代理"**。客户端把 OpenAI 兼容请求发给网关，网关自动完成：记忆召回（recall，把相关记忆块附加到最新 user 消息末尾）、已注入内容的字节级重放（replay，保住上游 prompt cache）、对话保存与后台记忆抽取（save/commit）、长对话压缩（compaction），还能在网关侧代跑 `openviking_find/read/grep` 工具循环（客户端无需任何工具支持）。**MaiBot 零代码、纯配置即可获得完整的记忆读写闭环**——这是该网关对 MaiBot 场景最大的价值。
2. **推荐方案 A：把 `replyer` 任务池整体切到 OV 网关 provider**（上游挂 wo4 现有 new-api），planner/utils/embedding 等其余任务维持直连不动；A_memorix 保留（人物画像、图片记忆、WebUI 管理继续用），关掉其聊天记忆自动注入避免双重注入。
3. **前置待办已完成（2026-10-20 当日闭环）**：已新建独立账户/用户 `maibot`（与个人 `default` 账户完全隔离，网关侧 upstream/policy/key 全部挂在 `maibot` 账户下），签发网关密钥 `ovgw_…`（保存于 wo4 `/root/openviking/maibot-gateway-key.txt`，chmod 600），new-api 上游连通测试 200；**记忆闭环冒烟通过**——同一会话两轮记住两件事，静默 commit 抽取后，新会话提问两个答案全部准确召回。
4. **两个必须提前认账的风险/坑**：① **sidecar 模式下网关路由挂在根路径**（`http://10.126.126.2:1935/v1/...`），文档默认的 `/gateway/v1` 前缀是嵌入式（与服务器同端口）模式的；打错路径会被网关 catch-all **静默直通转发给上游**（实测表现为上游报 Invalid URL，请求日志记为 passthrough）。② MaiBot 请求层只有 per-provider 静态 `default_headers`，**绝不能设静态 `X-OpenViking-Session`**（会把所有群聊合并成一个网关会话），MVP 靠网关的"历史匹配"识别会话，P2 用 `@LLMProvider` 插件加动态 header；③ 单一密钥 = 全 bot 共享一个记忆空间，跨群记忆互通（A 群的私聊话题可能被召回进 B 群回复），按群隔离需要每群一个 OV 用户+密钥，而任务池选模不感知 chat，同样要走插件级路由；④ beta 软件（SQLite 单机、无故障转移）进入回复主链路，recall 使每条新消息首包最多 +2s，网关挂掉时 replyer 池没有降级模型，回滚靠配置备份。
5. **顺手发现部署遗留**：gateway 容器 docker 状态 unhealthy，原因是 healthcheck 仍在网关容器内 curl `127.0.0.1:1933`（嵌入式模式的假设），sidecar 架构下应指向服务器容器地址或网关自身 `/health`。功能本身正常（实测 `/health` 返回 openviking ok/healthy/auth_mode=api_key），建议改 compose 覆盖 healthcheck。

---

## 2. OpenViking 网关机制（v0.5.0 Beta）

### 2.1 定位

OpenViking 本体是"AI Agent 上下文数据库"（volcengine 开源，AGPLv3，40k+ stars），把记忆/知识/技能组织成 `viking://` 虚拟文件系统。网关（v0.5.0 新增，Beta）是它的一种交付形态：**插在客户端与模型 API 之间的代理**，让任何只能发 API-key 请求的客户端（无法装插件/MCP 的）也获得记忆。wo4 上以独立 sidecar 容器部署。

### 2.2 网关处理一轮请求的五个步骤

| 步骤 | 行为 | 关键细节 |
| --- | --- | --- |
| Recognize | 识别请求属于哪个会话、是新用户消息还是工具步/杂务请求 | 见 2.4 |
| Replay | 把它之前注入过的记忆块按字节原样放回对应消息 | 保上游 prompt cache 前缀稳定 |
| Recall | 仅对新用户消息：用消息文本检索 OpenViking，把结果块附加到该消息**末尾** | 默认限时 2s，超时/宕机则**不带记忆放行**；每条消息只检索一次（客户端重试不重复检索）；每消息/每窗口有注入预算 |
| Forward | 以请求进来的同一 API 协议转发给该密钥绑定的上游 | new-api 作为上游时对 MaiBot 透明 |
| Save | 把完成的轮次排队，**落后一轮**保存（下一条用户消息确认客户端保留了上一轮才存） | 静默 10 分钟后保存最后一轮；保存后 commit（静默 10 分钟或积压 20k tokens 触发），OpenViking 后台抽取记忆 |

新会话首请求还会注入 **opening context**：用户 profile + 记忆/技能目录（4k token 预算），这部分不占 recall 预算。

### 2.3 端点与鉴权（源码确认）

从 wo4 容器内运行代码（`/app/.venv/lib/python3.13/site-packages/openviking_gateway/app.py`）确认的路由：`/v1/models`、`/v1/chat/completions`（Chat Completions）、Anthropic Messages（`/gateway` 根路径）、`/v1/responses`（Responses，含 WebSocket）、Ark 兼容路径（`/api/v3/*`、`/api/compatible/*`）、管理面 `/admin/*`（overview/keys/upstreams test/数据删除等）、`/gateway/uploads`。

MaiBot 走 **Chat Completions**：

```
Base URL: http://10.126.126.2:1935/v1     ← sidecar 模式挂在根路径，没有 /gateway 前缀
API key:  ovgw_…（网关密钥，Bearer）
模型名:   原样透传给上游（上游 model 列表为空时接受任意模型名，/v1/models 返回空列表属正常）
```

> **路径坑（实测踩过两次）**：`/gateway/v1/...` 是嵌入式模式（网关与服务器同端口）的地址形式。sidecar 独立端口部署时路由在根路径；打到不存在的路径不会 404，而是被网关 catch-all **静默直通转发给上游**（上游返回 OpenAI 风格 Invalid URL，请求日志记为 `passthrough`），排障时容易误判成上游问题。
>
> **2026-10-21 实际接入时踩的第二种形态**：MaiBot 侧 base_url 写成 `http://10.126.126.2:1935`（**少了 `/v1`**）——MaiBot 的 OpenAI 客户端不会自动补 `/v1`，实际请求打到 `/chat/completions`，网关同样不认识该路径，按未知端点**静默直通转发**给上游。表现为：回复完全正常（上游 new-api 照常服务）、但零 recall、零保存，"接入了却不记记忆"。**排障签名**：网关日志（注意查密钥所属账户的日志，管理 API 必须带对的 `X-OpenViking-Account` 头）出现大量 `kind=passthrough` 且带 model 名、recall/capture 字段为空的条目 = 客户端路径不对。修复：base_url 补 `/v1`，主程序配置热重载会自动清空 LLM 客户端缓存（日志「检测到配置重载，已清空LLM客户端实例缓存」），无需重启；生效后日志变为 `kind=user` + `capture=active`。

鉴权链：网关密钥 `ovgw_` → 绑定一个 OpenViking 用户（记忆归属者）+ 一个 context profile + 上游。上游（provider 协议 + base_url + provider API key）由 Studio/管理 API 配置，**客户端永远只见网关密钥**。网关转发前会剥掉所有 `X-OpenViking-*` 头。

### 2.4 会话识别（MaiBot 集成的关键约束）

网关按以下顺序取第一个存在的头：`X-OpenViking-Session` → `thread-id` → `x-claude-code-session-id` → `x-opencode-session-id` → `x-session-id` → `session-id`。**都没有时，取历史里最后一条 assistant 回复去匹配唯一的历史会话；匹配不上就开新会话**。

- 未加入已有会话的请求**依然有记忆**：只要消息本身能匹配到此前注入过的内容就重放，新消息照样召回；差别是这轮会存进新的 OV 会话、窗口预算从零开始。
- 会话按「用户 × API 协议」隔离；同一用户两个密钥发同一 session 值是同一会话。
- **对 MaiBot 的含义**：MaiBot 请求层只有 per-provider 的静态 `default_headers`（`config/model_config.toml` 的 api_provider 段），没有 per-chat header 通道。设静态 session 值 = 所有群聊合并成一个会话（保存串味、预算共享），**禁止**。MVP 依赖历史匹配：MaiBot 每轮重建完整 prompt、含 bot 历史回复，最后一条 assistant 回复大概率能唯一定位群聊。需 P0 在 Studio Requests 页验证匹配正确率。

### 2.5 保存与记忆抽取

- 保存内容会**剥离网关注入块和客户端噪声（如 `<system-reminder>`）**；但 MaiBot prompt 的其余脚手架（人设 system prompt、表达方式、额外回复要求块）会随会话存入 OV——记忆抽取质量会被这些噪声稀释，属于接受项（抽取是 LLM 归纳，噪声有界）。
- 客户端改写历史（编辑/删除/重新生成已保存轮次/自行压缩）→ 网关把当前完整历史存为新会话，已存轮次留在旧会话。
- 抽取在 OV 服务端后台进行，用的是 OV 自身配置的 VLM/embedding（wo4 上 ZCode 已在用，有持续 token 消耗）。

### 2.6 提示注入物与网关工具（QQ 场景注意项)

- recall 块形如 `<openviking-context source="gateway-recall">`，模型可见、不出现在回复里；opening context 形如 `gateway-session-start`。
- **网关工具**：开启后网关把 `openviking_find/read/grep/remember…` 注入请求的工具列表（客户端工具列表不变），模型调用时网关代跑、结果回灌、继续生成，下一轮原样重放这些轮次。**每次调用会在回复正文里插一行通知**（如 `> OpenViking find: "…" — done`，仅用户可见、模型不可见）——**这行会直接出现在 QQ 消息里**。context profile 的 "Show tool calls" 必须关掉，或 MVP 干脆不开工具。
- "Show recall summary"（回复开头加一行摘要）默认关，保持关。

### 2.7 与 MaiBot 场景相关的行为代价（精选）

| 行为 | 影响 | 对策 |
| --- | --- | --- |
| recall 等待最多 2s（每条新消息首次模型调用） | 回复延迟 +0~2s | profile 可调 Time limit；或先关 recall 只做保存 |
| profile/上游变更只对**新会话**生效 | 调参后旧会话不生效 | 观察时注意；必要时换 session 值（MaiBot 场景换配置≈自然新会话） |
| 压缩在 90% 窗口时触发，由"同一模型"写摘要 | 触发轮多一次计费调用 | profile 设准 context window |
| 一个密钥一个记忆归属者 | 跨群共享记忆（见 §6.6） | MVP 接受；P2 每群一密钥 |
| 请求体 >32MiB 拒绝 | MaiBot prompt 远达不到 | 忽略 |
| 嵌入等非模型端点直通不带记忆 | embedding 不受影响 | 忽略 |

---

## 3. wo4 部署现状（2026-10-20）

- OpenViking v0.5.0 独立 sidecar 架构：`openviking`（1933，healthy）+ `openviking-gateway`（1935，端口绑定在 `10.126.126.2:1935`，仅 EasyTier 内网可达，未暴露公网——对 bot 场景是好事）。
- 实测 `http://10.126.126.2:1935/health` → `{"status":"ok","service":"openviking-gateway","openviking":{"status":"ok","healthy":true,"version":"v0.5.0","auth_mode":"api_key"}}`。
- **docker 状态 unhealthy 是 healthcheck 误配**：`openviking-entrypoint --healthcheck` 在网关容器内 curl `127.0.0.1:1933`（嵌入式假设）。建议 compose 里覆盖：
  ```yaml
  healthcheck:
    test: ["CMD-SHELL", "curl -sf http://127.0.0.1:1935/health || exit 1"]
  ```
- 配置底子：`/root/openviking/.env`（`OPENVIKING_GATEWAY_ADMIN_TOKEN` + Fernet 密钥）、`ov.conf` gateway 段、备份 `*.bak.gateway-20261020`。
- **未完成 → 已完成（2026-10-20 当日）**：原待办「添加上游、签发密钥」已全部走 API 完成，Studio 仅需后续可视化调整：
  - OV 服务器侧（root key，`POST /api/v1/admin/accounts`）：新建账户 `maibot` + 管理用户 `maibot`（与个人 `default` 账户隔离，user_key 仅在创建响应中出现一次）；
  - 网关侧（`Bearer <OPENVIKING_GATEWAY_ADMIN_TOKEN>` + `X-OpenViking-Account: maibot` 双头）：
    - `PUT /admin/policies/maibot-chat`：recall/capture 开、`gateway_tools=false`（工具通知行不进 QQ 消息）、`show_recall=false`、`idle_seconds=600`（测试时临时 60 加速 commit，已调回）；
    - `PUT /admin/upstreams/newapi`：protocol=chat、base_url=`http://10.126.126.2:30001/v1`（new-api，容器内可达）、vendor=generic、models 留空（接受任意模型名）；`POST /admin/upstreams/newapi/test` → 200；
    - `POST /admin/keys`：请求体带 **maibot 的用户密钥**（网关用它向 OV 服务器解析身份并绑定 user_id）→ 签出 `ovgw_…`，落盘 `/root/openviking/maibot-gateway-key.txt`（600）。密钥按用户密钥管理惯例后续应归档 Bitwarden。
  - 管理面注意：所有 `/admin/*` 调用都需要 `X-OpenViking-Account` 头指明账户，多账户（未来按群隔离）时同一网关可并存多套 upstream/policy/key。

---

## 4. MaiBot 接入面（代码调研结论）

### 4.1 LLM 请求层：任务池制，per-task 可独立指路

- 配置三级：`[[api_providers]]`（base_url/api_key/client_type/auth_type/**default_headers**/timeout/retry）→ `[[models]]`（name + model_identifier + api_provider）→ `[model_task_config.<task>]`（model_list + 策略 + hard_timeout）。任务名是**固定枚举**（replyer/planner/memory/mid_memory/utils/fast_model/learner/expression_use/emoji/vlm/voice/embedding 等，`src/llm_models/utils_model.py:133-159`），不能新增。
- **给某个任务单独换 base_url 无需改代码**：新建 provider + model，把模型放进该任务池即可（样例见 §6.3）。空池有回退链（如 mid_memory→fast_model→utils），指向网关时要把池填满，避免请求被悄悄导回别的任务。
- MaiBot 每轮为 replyer **重建完整 prompt**（Context Items，含 system/历史/bot 旧回复）——恰好符合网关"客户端每轮重发全量历史"的核心假设；重试请求（同历史）会被网关识别为同一消息不重复召回。

### 4.2 现有记忆体系（A_memorix）与注入链路

- 宿主直连（非插件）：门面 `memory_service`（`src/services/memory_service.py:92`），统一走 `a_memorix_host_service.invoke()`。检索 `search()`、写摘要 `ingest_summary()`、写原文 `ingest_text()`、人物画像 `get_person_profile()` 等。
- 自动注入点：`HeuristicMemoryInjector`（`src/maisaka/memory/heuristic_injector.py:47`）+ 人物画像 + 图片记忆，作为一次性 user 消息注入 **Planner** 请求（`src/maisaka/reasoning_engine.py:266-318`）；开关 `a_memorix.integration.heuristic_memory_recall_enabled`（`src/config/official_configs.py:1307`，模板默认 False，**wo4 实际值需核对**）。
- LLM 主动检索：内置工具 `query_memory` / `query_person_profile`（开关 `enable_memory_query_tool`）。
- 写回链路：发送成功后聊天摘要（`ChatSummaryWritebackService`，`src/services/memory_flow_service.py:509`）+ 人物事实抽取（`PersonFactWritebackService`）→ A_memorix。
- **共存结论**：A_memorix 与 OV 网关的记忆职责可分——A_memorix 管人物画像/图片记忆/图谱管理（网关不提供这些），OV 管"会话级长期记忆"（自动保存+抽取+召回）。开启网关后应关掉 A_memorix 的启发式聊天记忆注入（避免同一轮 prompt 里两套记忆块），`query_memory` 工具可先保留让 planner 主动检索旧体系，观察后再收敛。两套写回（A_memorix 摘要 + OV 抽取）短期并行，token 成本翻倍的部分主要是各自后台任务，可接受，后续按抽取质量二选一。

### 4.3 插件兜底通道（P2 演进用，MVP 不需要）

- `maisaka.replyer.before_request` hook 可改 `extra_prompt`/`task_name`/`model_name`；`maisaka.replyer.after_response` 可读写回复；`send_service.after_send` 可拿发出去的消息——插件直连 OV server API 自建 recall/write 是方案 B 的基础。
- `@LLMProvider` 可注册自定义 client_type（先例 `plugins/anyul-gemini-embed`），使某任务池整体走插件协议——这是实现 **per-request 动态 `X-OpenViking-Session`（按群路由密钥）** 的唯一干净通道（`src/plugin_runtime/host/supervisor.py:1203-1222`）。

---

## 5. 方案对比

| | **A. 纯配置：replyer 走网关（推荐 MVP）** | B. 插件直连 OV server API | C. @LLMProvider 协议级接管 |
| --- | --- | --- | --- |
| 改动量 | 仅 model_config.toml | 新插件（recall/save/commit 自编排） | 新插件 + 自定义协议客户端 |
| 记忆闭环 | 网关全包（召回/重放/保存/抽取/压缩/工具） | 全部自己写（含 prompt cache 友好注入、落后一轮保存语义） | 同 A（网关能力全保留） |
| 会话识别 | 历史匹配（够用，需验证） | 完全自控（chat_id 即 session） | 动态 header，完全自控 |
| 按群隔离 | 不支持（单密钥） | 支持（按 chat_id 定位用户空间） | 支持（按 chat 换密钥） |
| 风险面 | 网关在回复主链路（beta/SQLite/单机） | 插件 bug 面；召回质量取决于自建检索策略 | 同 A + 插件协议层 |
| 适用阶段 | 现在 | 不推荐（重复造网关的轮子） | P2（隔离/动态 header 需求坐实后） |

**结论**：先 A 后 C。B 是"不用网关用服务器"的路线，与"使用网关"的目标相悖，且要把 recall 预算、replay、落后保存这些细节重新做对，不值。

---

## 6. 推荐方案详设（方案 A）

### 6.1 数据流

```
QQ 群/私聊消息（SnowLuma 适配器）
   ▼
MaiBot 宿主（maim-bot-core 容器，wo4）
   ├─ Planner：直连 new-api（不变，A_memorix 画像/图片记忆注入不变）
   └─ Replyer：POST /v1/chat/completions（完整 prompt）
        ▼  http://10.126.126.2:1935/v1 （Bearer ovgw_…；sidecar 模式根路径）
OpenViking Gateway（sidecar，1935）
   ├─ Recognize/Replay/Recall（≤2s）/Save 队列
   ├─ 新会话：注入 maibot 用户的 profile + 记忆目录
   └─ 转发 ▼
new-api（上游，wo4）→ 各模型通道 → 回复原路返回
   └─ 后台：会话 commit → OpenViking 抽取记忆 → 下次 recall 命中
```

### 6.2 网关侧配置（已于 2026-10-20 走 API 完成，Studio 可视化管理等效）

实际执行的 API 链路（详见 §3），Studio 操作与此一一对应：

1. **建独立账户/用户**：root key `POST http://10.126.126.2:1933/api/v1/admin/accounts`，body `{"account_id":"maibot","admin_user_id":"maibot"}` → 响应含该用户 `user_key`（只显示一次）。
2. **上游**：`PUT /admin/upstreams/newapi`：Chat Completions、Base URL 填 new-api 的 `/v1` 地址、API key 建议用 **new-api 独立子令牌**（当前测试暂复用 MaiBot 现用 token，生产前应换）→ `POST /admin/upstreams/newapi/test` 出 200。
3. **Context profile**：`PUT /admin/policies/maibot-chat`：gateway_tools 关（工具通知行会进 QQ 消息；P2 再开 read-only 且关 Show tool calls）、show_recall 关、recall Time limit 1.5~2s、`idle_seconds=600`。
4. **签发密钥**：`POST /admin/keys`：body `{"name":"maibot-test","openviking_key":"<maibot 用户密钥>","policy_id":"maibot-chat","upstream_ids":["newapi"]}` → 复制 `ovgw_…`（只显示一次）。

### 6.3 MaiBot 侧配置样例（model_config.toml，改前备份）

```toml
[[api_providers]]
name = "OpenVikingGateway"
base_url = "http://10.126.126.2:1935/v1"   # sidecar 模式根路径；嵌入式才是 /gateway/v1
api_key = "ovgw_xxx"
client_type = "openai"
auth_type = "bearer"
# 注意：不要在这里设静态 X-OpenViking-Session（会把所有会话合并成一个）
default_headers = {}
reasoning_parse_mode = "auto"
max_retry = 1        # 网关自身已有幂等语义，少一层重试
timeout = 150        # recall 等待 + 模型响应，略放宽

[[models]]
model_identifier = "<new-api 中的真实模型名，原样透传>"
name = "ovgw-reply-model"
api_provider = "OpenVikingGateway"
# 如需多模型降级，可再定义多个 [[models]] 都指向 OpenVikingGateway
# （上游 model 列表留空时接受任意模型名）

[model_task_config.replyer]
model_list = ["ovgw-reply-model"]   # 整池切换；回滚 = 恢复备份 + 重启
max_tokens = 2048
temperature = 0.3
selection_strategy = "sequential"
hard_timeout = 240.0
```

配置模板机制注意：仓库内只改 `config/model_config.toml` 模板并升版本号是给上游 PR 用的；wo4 实操直接改容器挂载的现网 `model_config.toml`，改前 `cp model_config.toml model_config.toml.bak.ovgateway-<日期>`。

### 6.4 会话识别策略与验证

> **2026-10-22 更新：会话识别已由插件 `anyul.ovk-gateway` 彻底解决，下述"历史匹配 MVP"方案作废。**

- **最终方案（已部署 wo4 并验证）**：插件用 Hook `maisaka.replyer.before_model_request` 把聊天流 ID 包成标记 Item 追加到请求末尾，自定义 client_type `anyul.ovk-gateway` 转发前剥出标记、删除标记 Item、换算成 `X-OpenViking-Session` 请求头——每个群聊/私聊独立稳定会话。插件路径可行性的两个关键前提（本报告此前误判"插件方案走不通"）：① replyer 请求不自带 stream handler/response parser（`maisaka_generator_base.py:1215` 只传 model_name），`PluginLLMClient` 的拒绝条件不触发；② 会话 ID 虽不在请求快照里，但 hook 参数里有 `session_id`，经 items 标记跨 RPC 传递即可。源码在 MaiBot 仓库 `plugins/anyul-ovk-gateway/`（core 纯函数 + SDK 胶水 + 9 个单测）。
- **部署迭代记录**：v0.1.0 首版踩了 SDK HookHandler 返回形状坑——runner 只透传 `action`/`modified_kwargs`/`custom_result` 三键（`runner_main.py:2341`），items 放顶层被**静默丢弃**（无报错、宿主用原始 items），症状即"还是形成新会话"；v0.1.1 修正为 `modified_kwargs: {items: [...], item_schema_version: ...}` 并在注入成功时打 INFO 日志「已注入会话标记」。诊断签名：provider 出现「未发现会话标记」警告 + 网关日志仍是 `anonymous-*` = 标记通道断了；hook 侧「已注入会话标记」INFO + 网关会话稳定 = 正常。
- **网关的两层会话机制（v0.1.1 生效后仍见"新会话"的第二层根因）**：网关内部有两层——① **conversation 层**（会话头/历史匹配决定 recall、replay、保存调度，按 account+user+protocol+session 值归属）；② **OV 存储层**（Studio Sessions 里的 `gateway-…` 文件）。保存规则是"落后一轮"，且**客户端改写了历史时，把当前完整历史另存一个新 OV 会话**（旧轮次留在旧会话）。recall 按**用户记忆库全局**检索，与 session 文件无关；replay 按 conversation（sid）工作，也与 session 文件无关——所以 session 分叉**不影响记忆连续性**，但每轮分叉会让每个快照都触发一次重复的记忆抽取（token 浪费 + Studio 噪音）。
- **第三层根因（prompt-guard 随机 nonce）与修复**：对比同一群相邻两次 prompt 落盘，37 条 item 仅 15 条文本原样再现——差的恰是 prompt-guard 包了 `untrusted_chat` 的 ~20 条历史：其 nonce 每次请求随机生成（`secrets.token_hex(3)`），导致每条历史每轮都"变了"，网关每轮都按"改写历史"另存新会话（一夜累计 135 个）。已在 prompt-guard v1.0.1 修复：nonce 改为 `HMAC-SHA256(启动期随机盐, 被包裹内容)` 派生前 6 位——同内容跨请求稳定（历史字节级不变，不再每轮分叉），盐值攻击者不可知（为注入内容计算合法闭合标签仍不可行，防伪性质保留）。**修复后实测 36/41 原样命中，wrapped 条目已全部稳定。**
- **第四层结论（结构性边界，2026-10-22 定案）：剩余的每轮分叉无法在插件层修复。** 网关的会话锚点是**链式哈希**（`protocols.py prefix_chain`：每条消息锚点 = sha256(前一条锚点 + 规范化消息)，类似 git）；`capture.py advance()` 的分叉判据是"最后已投递消息（= 网关中继的 assistant 回复）的锚点必须出现在本次请求里"。而 MaiBot 是"每轮重建 prompt"模型：① 从不重发 assistant 角色回复（bot 回复以 user 条目形态回历史）；② 每轮的"目标消息块"（含当前时间/msg_id）下轮即弃；③ 滚动窗口淘汰旧消息。链条中段必断，锚点全废——除非插件自持"规范历史"整体替换 MaiBot prompt（会破坏窗口管理与每轮指导语义，且锚点规范化细节需逐字节对齐，得不偿失）。**定案：接受存储层每轮分叉为 MaiBot+网关组合的运行形态**，记忆功能不受影响（recall 按用户记忆库全局检索；抽取正常出活，实测当日 20 条记忆文件）。代价=每轮一个 ~16KB 快照 + 每快照一次重复抽取（idle 600s commit）。可选减损：调大 policy `idle_seconds` 降 commit 频率；定期清理 session 文件；正解在上游（MaiBot 重发 assistant 历史或 OV 容忍 rebuild 型客户端）。
- **契约验证（2026-10-22 03:27 实测）**：同一 `X-OpenViking-Session` 连续两轮 → 网关日志两条请求落同一 conversation（`269e4b…`，不再是 `anonymous-*`）、第二轮 `replay=1`（注入内容字节级重放）、`capture=active`。
- **历史匹配方案的劣化场景存档**（已在实践中应验）：MaiBot prompt 全部是 `UserMessageItem`（38 个 item 无一 assistant 角色），网关 `kernel.py` 的 identity 匹配器找不到 assistant 锚点，每次请求开新 anonymous 会话，recall 只有碎片、保存零散入账——这就是"每次回复都是新会话、记忆没有召回"的根因。

### 6.5 A_memorix 共存清单

1. 核对 wo4 `bot_config.toml [a_memorix.integration]`，将 `heuristic_memory_recall_enabled` 设为 false（若已开）。
2. `enable_memory_query_tool` 先保留（planner 主动检索旧记忆），观察两套记忆是否互相干扰再定。
3. 人物画像注入、图片记忆不动（网关不覆盖）。
4. WebUI 记忆页/A_memorix 管理全部不动。

### 6.6 记忆隔离（如实陈述的取舍）

- MVP 单密钥 = 单 OV 用户 = 全 bot 一个记忆空间。当前 wo4 规模小（1 bot 2 群），接受"跨群记忆互通"（也可以视为 bot 的统一人格记忆，未必是缺陷）；**私聊若接入则必须先隔离**。
- 按群/按私聊隔离路径（P2）：每群一个 OV 用户 + 网关密钥（Studio 手工或 `/admin/keys` 脚本化）+ `@LLMProvider` 插件按 chat 路由密钥。wo4 本就双 bot 实例运行，实例级隔离（每实例一份 model_config/密钥）是零代码的中间态。

### 6.7 端到端验证清单

1. ~~`curl …/v1/models`~~ ✅ 已通过（返回空列表属正常：上游 models 留空=接受任意模型名）；
2. ~~两轮对话同会话~~ ✅ 已通过（`X-OpenViking-Session: smoke-1` 两条请求落到同一会话、Saving On、出现 `gateway-…` 会话）；
3. ~~静默过 commit 窗口后新会话考记忆~~ ✅ 已通过（smoke-2 新会话问"测试代号/喜欢的测试水果"，两个答案全部准确召回）；
4. 切 replyer 池 → 群里正常聊几轮 → Requests 页核对会话归属与召回计数（+N）→ QQ 侧确认回复无 `<openviking-context`、无工具通知行；
5. 压测一项：recall 开启后 replyer `slow_threshold`（25s）日志无新增告警。

---

## 7. 风险清单与对策

| # | 风险 | 等级 | 对策 |
| --- | --- | --- | --- |
| 1 | 历史匹配串会话（重试/相同回复/多群同文） | 中 | P0 用 Requests 页验证；劣化则 P2 动态 header 插件 |
| 2 | 跨群记忆互通/隐私 | 中 | MVP 接受（1 bot 2 群）；私聊接入前必须做隔离（P2） |
| 3 | 网关 beta + SQLite 单机在回复主链路，无降级模型 | 中 | 上游用 new-api 子令牌（new-api 自身多通道容错）；回滚=SOP：恢复 model_config 备份+重启；网关监控走既有巡检 |
| 4 | recall +2s 首包延迟 | 低 | profile 调 Time limit；或先只开保存 |
| 5 | 记忆抽取被 MaiBot prompt 脚手架噪声稀释 | 中 | 观察抽取质量（Studio Sessions 页读记忆文件）；必要时调整 profile 或后续评估 A_memorix 与 OV 二选一 |
| 6 | 双记忆体系后台 token 成本（OV 抽取 + A_memorix 摘要/事实） | 低 | 都走 new-api 计费通道可观测；按质量收敛后砍一套 |
| 7 | 工具通知行/召回摘要漏进 QQ 消息 | 低（配置可根除） | profile 关 tools（或关 Show tool calls）、关 Show recall summary |
| 8 | static header 合并会话（误配置） | 低（一次性坑） | 本报告 §6.3 已标红；配置评审时检查 |
| 9 | gateway 容器 unhealthy 误报干扰巡检 | 低 | 修 compose healthcheck（§3） |
| 10 | watchtower 更新 MaiBot/网关镜像引入行为变化 | 低 | 网关与服务器同仓同版本升级； MaiBot 侧配置与代码解耦，回滚容易 |

---

## 8. 实施路线图

- **P0 网关收尾 + 冒烟（半天）——✅ 已于 2026-10-20 完成**
  1. ~~修 compose healthcheck~~（未做，仍建议：消除 unhealthy 误报）；
  2. ~~建 `maibot` 账户/用户 → policy（tools 关）→ new-api 上游 → 签发 `ovgw_` 密钥~~（已走 API 完成，见 §3）；
  3. ~~curl 三步冒烟，确认召回/保存/抽取闭环~~（已通过，见 §6.7）。
  - 遗留：healthcheck 修正；new-api 上游换独立子令牌；`ovgw_` 密钥归档 Bitwarden。
- **P1 接入观察（1 天 + 数天观察期）**
  1. wo4 `model_config.toml` 备份 → 按 §6.3 切 replyer 池 → 重启；
  2. 核对 A_memorix 共存清单（§6.5）；
  3. 按 §6.4/§6.7 验证；观察 3~7 天：Requests 会话归属正确率、Sessions 抽取质量、QQ 主观效果、延迟与 token 消耗。
- **P2 演进（按需）**
  1. ~~`@LLMProvider` 插件：动态 `X-OpenViking-Session` + 按群/私聊密钥路由~~——**会话头部分已于 2026-10-22 提前落地**：`plugins/anyul-ovk-gateway/`（按群/私聊独立会话）；按群独立**密钥**（记忆空间隔离）仍未做，任务池选模不感知 chat 的限制依旧；
  2. 开启网关 read-only 工具（关通知行）让模型自主深读记忆；
  3. 根据抽取质量决定 A_memorix 聊天记忆链路的去留（画像/图片记忆预计长期保留）。

---

## 11. 插件化落地：anyul-ov-memory（2026-10-22）

> 网关方案的架构性局限确认后（§4 补充调研 + 本节），长期记忆改为**插件直连 OV server API** 的形态落地，仓库位于 `plugins/anyul-ov-memory/`（独立 git 仓库，v0.1.0）。本节记录边界判断结论，插件细节以其 README 为准。

### 11.1 触发因素：网关方案的两个结构性边界

1. **回复模型拿不到主动检索**。代码调研确认（`maisaka_generator_base.py:991` replyer 单次无工具调用；`:962` replyer 历史显式过滤 `ToolResultMessage`/`ReferenceMessage` 等；planner→replyer 唯一信息通道是 reply 工具必填参数 `reply_reference`）——replyer 挂网关只能获得被动 recall（末条 user 消息文本查询、2s 预算、单跳），planner 的检索结果也天然进不了 replyer。主动检索的正确位置是 **planner 工具循环**（`query_memory`/`subagent` 既有模式），经 `reply_reference` 流入回复器。
2. **网关存储会话无法收拢**。§6.4 第四层已定案链式哈希锚点 vs 每轮重建 prompt 的矛盾不可在客户端修复。收拢为单会话的唯一出路：绕开网关，直连 OV server session API。

### 11.2 插件设计（对照官方 pi 集成与 ZCode 集成）

- **写路径（capture→commit）**：`chat.receive.after_process` + `send_service.after_send` 捕获真实聊天文本（群聊带 `[说话人]：` 前缀；过滤通知/命令/占位），按聊天流映射为**稳定 OV 会话**（`maibot-<session_md5>`，每群聊/私聊恰一个），单写者串行写入（保序+退避重试）；空闲 600s 或积压 80 条触发 commit → OV 后台抽取。写入只含真实对话文本，网关方案的脚手架噪声与每轮快照/重复抽取一并消除。
- **读路径（recall）**：`@Tool ov_memory_search`（core_tool，find QUICK 模式低延迟）+ `@Tool ov_memory_read`（行分页深读），planner 按需主动检索；不做每条消息自动注入（既定偏好：省 token、行为可控）。
- **运维**：`/ovmem`（状态/强制提交）命令；失败语义为退避重试→超限丢弃并 ERROR 暴露，不静默兜底。

### 11.3 部署切换路径（替代原 P1）

1. 签发/取回 maibot 用户 **OV user key**（不是 `ovgw_` 网关密钥），填入插件配置；
2. 部署插件并验证（`/ovmem` → 聊几轮 → Studio Sessions 出现 `maibot-…` 会话且抽取出活 → 次日考记忆）；
3. 验证通过后 replyer 池从网关切回 new-api 直连（`model_config.toml` 回滚 §6.3 改动），`anyul.ovk-gateway` 插件退役（可留作回滚）；**网关时代抽取的记忆文件不受影响**（recall 按用户记忆库全局检索）；
4. A_memorix 共存策略不变（§6.5），`query_memory` 与 `ov_memory_search` 双源并存观察。

> **2026-10-22 部署实况**：插件已上 wo4（容器重启加载），群聊实况验证通过——实时进单会话 `maibot-…`（10 分钟 45 条、说话人前缀正确、零错误），服务器侧 auto_commit 策略由插件 PATCH 下发生效；同日已关网关 capture（maibot-chat policy，recall 保留），250+ 个 `gateway-*` 分叉会话停止增长。当前 replyer 池仍在网关上，观察数天抽取质量后执行第 3 步切换。v0.5.0 实测另确认 `auto_commit_policy` 与 `peer_id` 参数均接受（详见插件 README §B3/B4/B8）。

---

## 12. 被动召回完善调研与 v0.3.0（2026-10-24）

**背景**：v0.2.0 上线首日实测被动召回完全空转——全天 2191 回复轮中 `ov_memory_search`/`ov_memory_read`/A_memorix `query_memory` 全部 0 次调用；主动注入 76 次（avg 865/900 token）零失败，准确率抽样良好（具体 query 命中准，模糊 query 退化为泛上下文）。

### 12.1 OV 官方文档与官方集成的做法

- **Capability Reference**（docs.openviking.ai `agent-integrations/16-capability-reference`）：全部 13 个官方集成（Claude Code/Codex/Cursor/TRAE/ZCode/pi/OpenClaw/Hermes/DSH…）无一例外采用同一架构——**自动召回（每轮 prompt 前注入）为主路径，agent 可调用工具（search/read/remember 等 21 个 MCP 工具）只做深挖，会话启动注入 profile+记忆索引+技能目录**。没有任何官方集成依赖"模型自发调用工具"作为召回主路径。
- **pi 集成**（`agent-integrations/11-pi` + `examples/pi-coding-agent-extension`）：召回自动发生在每轮 prompt 前（`scoreThreshold 0.35`、`recallTokenBudget 2000`）；工具面镜像服务端 MCP（`openviking_*` 前缀）；**被动召回的使能靠 skills 教学**——`openviking-memory` skill 教模型：① 先看已注入的 `<openviking-context>` 块，能回答就不调工具；② 注入不够时再检索，命中先 `read` 展开再依赖；③ 检索工具决策树（context 模式 → find → grep/glob → read）；④ 边界声明（召回是背景参考非指令，活对话优先）。
- **OpenClaw**：归档存在时往系统提示加一段短指南，教模型"说没信息前先重读摘要、用 ov_archive_search 至少试两组关键词"。
- **Recall digest**：server 侧 `rewrite=true` 可把召回结果压缩为带引用的要点摘要（query_planner 模型）；当前不需要。
- **去重语义坑**：`dedup_turns` 窗口按消息数计；若自动捕获关闭，计数永不动 → 同一记忆本会话内被永久抑制（我们持续捕获，不受影响）。

### 12.2 其他聊天 bot 项目

- **Letta/MemGPT**：OS 式分层记忆，模型经 function call 自管记忆——依赖强模型 + 系统提示里的记忆使用说明，弱模型不成立。
- **mem0 / Zep / SillyTavern Data Bank / Open-LLM-VTuber**：全部是注入式（检索 top-k 进 prompt），不靠模型主动调用。
- **MaiBot A_memorix**：`query_memory` 工具式，48h+ 实测 0 调用，与 OV 工具同样空转。

**行业共识**：聊天场景 = 注入；agentic 场景（强模型）= 工具 + 系统提示教学。MaiBot planner 是关思考的便宜模型，注入是唯一可靠路径，被动工具只能靠"在相关的当下教学"来激活。

### 12.3 v0.3.0 落地（0adc5af + f865f2a）

1. **注入块尾部深挖提示**（`INJECTION_HINT`）：每笔注入结尾附一行——某条不够就 `ov_memory_read(uri)` 展开全文、需换角度就 `ov_memory_search(query)`。把教学放在内容恰好相关的当下（对照 pi 的 skill 教学 + OpenClaw 的系统提示指南）。`recall.hint_enabled` 可关（省约 40 token/轮）。
2. **工具描述重写**：`ov_memory_search` 触发场景写明"自动召回概览不够 / 用户提及历史（上次/之前/还记得吗）"，并声明与注入块的关系；`ov_memory_read` 声明"uri 也可来自自动召回概览、概览可能比原文薄，依赖前先读全文"。
3. 31 测试全绿；部署时踩一次坑：config_model.py docstring 与 `__ui_label__` 被挤到同一行成语法错误，部署后加载失败一次（卸载 flush 正常零丢失），22:30 修复重载成功。

**观察期 KPI**：`search_tool_calls` 是否从 0 转正；若教学后仍持续为 0，则判定该模型档位下被动工具不可激活，维持注入单通道（工具保留在工具表，成本仅为工具表 token）。

---

## 9. 开放问题

1. MaiBot replyer prompt 里 bot 历史消息的 role 是否为 assistant（决定历史匹配的可用性），P0 实测直接回答，无需读码预判；
2. MaiBot 流式请求是否带 `stream_options.include_usage`——不带则网关只能估算窗口占用，影响 compaction 时机精度（接入后看 Requests 页 usage 是否为 0 即知）；
3. OV 抽取出的记忆文件质量对"会话级群聊"语料的适配度（网关主要为编码代理/聊天助手设计，群聊多说话人场景是未验证域）；
4. 若 P2 需要按群密钥：`/admin/keys` + root 用户 API 的自动化建号流程需要一次专项验证。

---

## 10. 参考资料

- 网关文档（15 页章节）：<https://docs.openviking.ai/en/gateway/01-how-it-works>（机制）· `02-quickstart` · `03-connect-clients`（各客户端 Base URL 表）· `04-openviking-tools` · `05-long-conversations` · `08-context-profiles` · `09-gateway-keys` · `13-configuration`
- 源码：`openviking_gateway/`（GitHub volcengine/OpenViking；wo4 容器内 `/app/.venv/lib/python3.13/site-packages/openviking_gateway/app.py` 为运行版本 v0.5.0 实况）
- MaiBot 侧（本仓库）：`config/model_config.toml`（api_provider/task 池字段）· `src/llm_models/utils_model.py`（任务枚举/回退链）· `src/maisaka/memory/heuristic_injector.py`、`src/maisaka/reasoning_engine.py`（现有记忆注入点）· `src/services/memory_flow_service.py`(写回链路) · `docs/插件开发指南.md`（hook/LLMProvider，P2 用）
- 运维：wo4 `/root/openviking/`（compose/ov.conf/备份）；本文 wo4 事实核对于 2026-10-20 实测
