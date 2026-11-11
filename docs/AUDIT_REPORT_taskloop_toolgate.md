# 自主任务链路审计报告（anyul.task-loop × anyul.mcp-toolgate）

- 日期：2026-11-11
- 依据：《自主任务链路审计方案》（docs/自主任务链路审计方案.md，四轨道全执行）
- 背景：2026-11-11 单日三起同根因生产事故（tl000182fe 期限误杀 / tl00026856 误闭环+工具被裁 / 11:20 喝水提醒立项失败），当日热修 v0.2.1 / v0.9.4 后开审计
- 结论：**热修未引入新缺陷（终审后）**；横向扫描发现 1 项 P1 结构性缺陷（任务卡轮 query 漂移）并已修复；数据回溯确认事故家族规模为「3 起实锤 + 至少 2 起历史同族受害轮」；L1 黄金探测已落地并上线即抓到 1 个交付链检索缺口
- 交付：task-loop v0.2.2（commit 426fdb9）、toolgate v0.9.5（3f338ab + 2818ff7）已部署 wo4（2026-11-11 16:23 重启，启动护栏验证通过）；L1 探测接入 6h 巡检 + gotify

---

## 0. 数据链路图（审计基线）

```
用户消息 → chat_loop_service → planner 请求
  └─ items（<message> 信封用户消息 + 注入条 + 任务卡 InternalContextMessage[无信封]）
       ├─ toolgate.toolgate_filter（BLOCKING·EARLY，单钩子内顺序段）
       │    account_gate（allowed_servers 通配）→ 语义裁剪（top_k/threshold/MMR/
       │    │   mcp_min_keep/entity 席/unscored 必保；保底并集 in-code 4 项）
       │    ├─［v0.9.5 新增］插件主动轮（<plugin_proactive_task> 信封在最近真实
       │    │   内容项）→ 跳过语义层（F1 修复）
       │    ├─ recent_tool 保底注入 → reminder_gate（deferred 清单改写）→ identity_gate
       │    └─ 审计：retrieval/calls JSONL（shadow 轮 kept 列=反事实 would_keep）
  ├─ planner → task_submit 立项 → task-loop 台账（open→running→delivered/failed）
  │    心跳 tick：due→chase→fail_overdue→expire；after_send 闭环（冷静窗 20s）
  └─ 出站回复 → after_send 观测闭环
```

## 1. 轨道 1：部署态对账（全部通过）

| 项 | 结果 |
|---|---|
| 版本对账 | 审计时线上=本地 HEAD（task-loop 7 文件 md5 全同 / toolgate 7 业务文件全同；仅 `_patch_wo4.py` 线上旧版——本次部署已覆盖为警示版） |
| config 全键 diff（K7） | task-loop：**0 固化**，21 键一致，新键 `closure_grace_seconds` 缺失→merge 自动补默认（健康）。toolgate：6 项分叉逐一判定——`semantic.always_keep` 7 项旧快照=K7 实锤病例（已被 in-code 并集缓解，运行时有效 11）；`probe.servers`/`account_rules`/`semantic.enabled=True`/`embed_backend=local` 为有意生产配置；`shadow_rate=0.1` 为评测期残留（实测采样率 0.085，每轮 +20k token，建议降至 0.02-0.05，**决策留用户**） |
| 启动护栏 | `always_keep=11`✓、探针校验 cosine=1.000✓、零 Traceback/ERROR✓；v0.9.5 起护栏阈值 ≤4，启动期一次性「rank 席仅剩 4」WARNING 属预期（生产恰坐天花板最后一格） |
| 备份卫生 | 12 个备份目录全在 /root/maim-bot/plugin-backups/✓；plugins/ 内无备份目录、无 tar.gz 残留✓；toolgate 目录 3 个 `config.toml.bak*` 快照化石已迁 plugin-backups/toolgate-config-fossils-20261111/ |
| 重启归因 | 13:23:33=v0.9.4 部署重启；16:23:22=v0.9.5/v0.2.2 部署重启；16:25:58/16:26:19 两波全量插件重载=部署文件触发宿主 file_watcher（既有行为，非缺陷），16:26:19 后零抖动 |

## 2. 轨道 2：热修 diff 复审（无「修 A 引入 B」；F-1 为残留路径非本轮引入）

| 复审点 | 结论 |
|---|---|
| 冷静窗随追讨重启→永不闭环 | **排除**：chase 间隔=l0 且下限 60s（plugin.py:279）> 默认 grace 20s；grace≥l0 误配域另立护栏（已修） |
| 窗外顺延 × deadline 公式 undercut | **排除**（数学不可达）：parse 永不返回过去值→被顺延的任务其语义到期点必然已过 |
| expire_tasks TTL / resume 对账 | **排除**；TTL 不抢杀远期语义任务（62e9d6b 修复落实） |
| `_tl_persist_state` 原子性/并发 | **排除**：tmp+os.replace；全写点在 Runner 单事件循环，纯同步 IO 无交错 |
| STATE_FILE… / task_submit 过去时间戳 / /tl 权限 | 全部排除（parse 无过去值；宿主 operator 拦截） |
| CLOSURE_CRITICAL_TOOLS 三处派生 | **通过**（真单一事实源；bench 经 SemanticSectionConfig 派生非 hardcode） |
| `_effective_always_keep` 热重载 | **证实缺陷 F-2（P2）→已修**：on_config_update 只 invalidate 向量缓存，改 always_keep/top_k 空操作直到重启 |
| rank 席护栏 | 逻辑通过；阈值 <4→≤4（生产 11 常驻/15 top_k 恰 4 席，护栏从永假临界变为有效提示） |
| **F-1（P0 处置）None 回退路径** | **证实**：中文数字钟点「明早十点」不匹配 `(\d{1,2})` → None → due=立项+5s、deadline=+45min、goal 拼「（软期限：原文）」被执行模型当真期限等待→tl000182fe 原形态逐字复现（分支 A 误杀 / 分支 B >20s 后 ack 假闭环绕过冷静窗）。62e9d6b 前即存在的残留，v0.2.1 在工具描述推广 hint 用法扩大命中面。**已修**：中文数字钟点解析 + 未解析 hint 如实标注「期限提示未解析」（tests 72 绿） |

## 3. 轨道 3：生产数据回溯（窗口 2026-11-04 ~ 11-11，2611 语义轮）

### 3.1 受害轮清单（词族×kept 对账，fate=同会话 240s 内行为）

| 时间 | 会话 | 形态 | 被裁工具 | 后果 | 判定 |
|---|---|---|---|---|---|
| 11-07 12:46:28/30 | bddc70a5 | 「三花提醒我10分钟后去打危机合约」 | task_submit | tool_search 绕行未果，当日零立项 | **实锤受害（回溯新发现）** |
| 11-10 15:19~15:23 | bddc70a5 | 「今天江苏多少摄氏度」「联网查啊」 | web_search/web_fetch | 「我这没联网」摆烂话术进 query 原文 | **实锤受害** |
| 11-11 11:20:32~12:21:49 | ca768949 | 喝水提醒+POE PN（K8 锚点） | task_submit（kept 176→22） | tool_search 绕行×3→纯 reply，无法立项 | 实锤（已知事故，方法验证✓） |
| 11-04 18:55 / 11-05 00:27 / 11-06 05:45~47 等 | — | 「之前/还记得」回忆式闲聊 | query_memory（保底前时代） | 摆烂/他工具调用 | 疑似（低危） |
| 11-11 03:45:44 | 25c8643d | 「定了，明天上午10点喊你打会战」 | —（shadow 轮全量在场） | 模型未调 task_submit | **模型行为缺口**（非裁剪；golden 第 6 条回归覆盖） |

- **误闭环存量**：窗口内 delivered 仅 tl00026856 一例，`delivered_at − triggered_at = 0.662s`（物理不可能完成查证+写档+发群）——ack 误闭环数值实锤；v0.2.1 冷静窗 20s 可拦，回归测试在。
- **立项漏斗**：插件上线 5 天 submitted=2（两起全是事故任务）；提醒类真实请求≥3 起，提醒立项成功率 **0/3**（11-07 危机合约、11-11 喝水被裁；11-11 会战在池未立项=模型行为）。
- **prompt 记录实证**：11:20:36 轮 tool_definitions=22 且无 task_submit（ledger kept=22 互证）；13:23 部署后轮 17 工具含四保底全在——v0.9.4 部署有效性在 prompt 层闭环。
- **取证教训（记入方法论）**：prompt 记录 tool_definitions 为嵌套 schema（`{"type":"function","function":{"name"}}`），首版探针取顶层 name 得 None 造成「四工具缺席」假警报——双账本对账 3 分钟内纠出；探针 schema 必须先对实物。

### 3.2 悬案裁决

- **K5（top_k 15→18）：维持 15。** 时代分界统计：v0.9.4 前（pre）保底工具挤出事件 31 次（task_submit 14 / query_memory 15 / web_search 2）+交付链 sandbox_upload_file 166 / send_voice_message 106；v0.9.4 后（post，13:23 起）**保底四工具挤出=0、受害轮=0**。判据①（挤出且该窗口需要）不满足；判据②（保底≥13）不满足。
- **K8 锚点**：方法有效（176→22、task_submit 缺席、tool_search 绕行全链复现）。
- **shadow_rate**：实测 0.085（配置 0.1）；**建议降至 0.02-0.05**（L1 探测已提供事前确定性断言，影子采样边际价值下降；+20k token/轮成本）——用户决策项。
- **P3 化妆品缺陷**：`dropped_by_rank` 可为负（entity 席不占 MMR 预算→kept_ranked>scored）；仅口径观感，监控无碍（semantic_gate.py:1001）。

## 4. 轨道 4：代码横向扫描（F1 P1 修复；F3/F4/F6/F8 留专项）

**已修（本次部署）**：
- **F1（P1，结构性）**：任务卡主动轮经 maisaka.proactive.trigger 以 InternalContextMessage 入历史（无 `<message>` 信封）→ extract_query 取漂移闲聊 → 按闲聊裁剪会把任务所需非保底 MCP 工具裁光 → 执行模型如实报告缺工具放弃（tl00026856 同族残余）。**修复**：`_has_proactive_trigger` 倒序识别宿主 `<plugin_proactive_task>` 信封→主动轮跳过语义层（账号过滤/身份链照常）；终审 Medium 收窄=只看「最近真实内容项」，防信封常驻历史导致后续普通轮持续 bypass。
- **F2（P2）**：`_effective_always_keep` 热重载不重算 → on_config_update 现重算有效集+护栏（runner 时序核实：宿主先应用配置再回调）。
- **F5（P3）**：expire_tasks abandoned 分支不可达（fail_overdue 每拍先行且条件更宽）→ 删除，签名收窄 int。
- **F7（P3）**：AuditStore.flush 跨线程迭代竞态 → 一次性快照拷贝（save_vectors 同款）。
- **F9/F-5/F-6（P3/Nit）**：拼写、bench 注释两方向化、config_version 0.5.0。
- **K4 补声**：remaining≤0 整池放行分支加逐轮 WARNING（原静默）；量化=当前安全余量 4 席，触发组合=常驻+4 或 top_k≤11。

**留专项（报告级，未修）**：
- **F3（P2，低频竞态）**：task-loop 配置热重载换 store 与在途 tick 的 await 竞态——触发记账写旧 store→任务卡重复派发。触发条件=配置保存恰好落在触发 RPC await 窗口（数秒/天级机会）。修复方向：tick 每拍开头重取 `self._tl_store` 或 swap 时 payload 重导。
- **F4（P2，设计取舍）**：lex 兜底在零信号 query（词表零重合）时按多样性盲裁到 top_k（~146 工具全裁）——有意设计但零信号时建议回退全量放行。
- **F6（P3，配置陷阱）**：`submit_delay_seconds > max(300, total_deadline×60)` 时任务 DOA（永不触发、到线判「未及启动」）；建议跨字段校验。
- **F8（P3，观察项）**：任务卡心跳轮 identity_gate 的 user_id 取「会话最近发言者」而非任务发起人（requester_id 在台账但宿主钩子拿不到）。
- **K6 代码侧**：task_submit 描述缺口口语词族（叫我/记得/别忘/蹲/抢）；task_status 贫瘠但非承重。已入保底集故裁剪面免疫，剩余影响在「是否选择立项」上游。

**通过项**：lex 全落空模式保底并集不失效（kept_always 分支前置证明）；embed 持续失败退化=4.0s/轮有界（<6s 熔断线）；三子闸门叠加顺序确认（同一 BLOCKING 钩子内 account→semantic→recent→reminder→identity）；`check_blocking_hook_kwargs.py` 两插件 0 ERROR。

## 5. L1 黄金探测（E2E 方案阶段一，已上线）

| 项 | 结果 |
|---|---|
| 组件 | `bench/golden_queries.json`（8 条：四起事故+交付链+第三人称形态+表达锚点+负样本）、`bench/probe_golden.py`、`test_probe_golden.py`（8 用例）、wo4 `scripts/e2e_run_l1.sh`（gotify 告警） |
| 保真 | 从 owner-table.json 取真实描述→指纹全命中生产 npz（终审 High 修复：空描述曾致全量重嵌入失真）；实体词索引显式关闭（重建语料不一致有偏松风险）；候选=缓存∩目录（300），257 个无描述历史存量向量排除并报告 |
| 真机验收 | EXIT=0：7 PASS + 1 XFAIL + 1 SKIP；喝水/危机合约/POE/会战 query 的保底工具全部存活；「唱歌」2 字 query 复刻 query_min_chars 旁路 |
| **首跑即发现** | 「帮我整理这个文档发群里」→ sandbox_upload_file 输掉 rank 竞争（kept_n=15 满席未入）——**交付链检索缺口实锤**（与 11:20 轮 dropped_top3 证据互证）。非当前静默失败（planner 仍有 files_send 直发路径），已标 xfail 持续观测；**处置建议（P2）**：sandbox 工具描述补「整理/文档/总结发群」词族（上游 K6 治理）或纳入保底集候选（需 criteria 评审） |
| 故障注入 | 测试层验收过：从 in-code 常量移除 task_submit→探测器必报缺失；顺带实证「配置层删保底无效」的防御强度 |
| 接线 | 6h 巡检自动化已加 B（双插件健康）/C（L1 探测）步骤；非零退出自动推 gotify（watchtower 通道复用） |

## 6. 终审与回归（全部闭环）

- code-reviewer 终审两仓 diff：REQUEST_CHANGES→全部采纳修复（2 High=probe CLI 缺 import 撞退出码契约+指纹保真；1 Medium=bypass 历史残留收窄；2 Low=stats 外层快照+分钟严格化；1 Nit=测试机制注释）→ 复审点逐条落实。
- 最终回归：task-loop 72✓ / toolgate probe 8 + audit 6 + entity 75✓ / `check_blocking_hook_kwargs` 0 ERROR✓ / py_compile 全过 / probe CLI 冒烟+真机 EXIT=0✓。

## 7. out-of-scope（注记）

宿主主程序与 A_memorix（含 EpisodeSegmentation 超时、metadata checkpoint）；prompt-guard 图片超帧遗留；maimai_sandbox 工具描述治理（属 K6 上游，跨插件）；L2 test 群合成对话 E2E（待测试 QQ 账号挂载点拍板）；shadow_rate 降频与 sandbox 检索缺口处置（用户决策项）。

## 8. 遗留专项清单（按优先级）

1. **F3 热重载换 store 竞态**（P2，低频）：修复方向已附（§4）
2. **sandbox_upload_file 检索缺口**（P2）：描述补词 vs 保底候选，二选一
3. **shadow_rate 0.1→0.02-0.05**（P3）：L1 上线后建议降频，改生产 config 一键
4. **F4 lex 零信号回退**（P2 设计确认）、**F6 DOA 跨字段校验**（P3）、**F8 心跳轮身份**（P3 观察）
5. E2E 方案阶段二（L2）待测试账号拍板；阶段三巡检集成两项已随本次上线（B/C 步骤）
