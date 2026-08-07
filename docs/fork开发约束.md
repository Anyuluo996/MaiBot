# Fork 开发约束

本文档是 `Anyuluo996/MaiBot` fork 的开发约定，覆盖代码风格、开发流程、分支策略和上游同步。
所有参与本 fork 的开发者（包括 AI 辅助开发）都应遵守。

> **与上游 AGENTS.md 的关系**：上游 `AGENTS.md` 已覆盖代码规范、import 顺序、注释/类型注解、配置文件修改、A_memorix 修改策略、prompt 模板多语言同步、WebUI 规范等。本文件**不重复**这些内容，只补充 fork 特有的约定。两者冲突时，以上游 `AGENTS.md` 为准。

---

## 分支策略

### 分支模型

- **`main`**：严格跟踪 `upstream/main`，不直接提交改动。只用于同步上游更新。
- **`feature/<功能名>`**：功能开发分支，所有改动在此提交。命名用 kebab-case，如 `feature/cache-optimization`。
- **`fix/<问题名>`**：bug 修复分支，如 `fix/replyer-time-overflow`。
- **`chore/<杂务名>`**：非功能性改动，如 `chore/update-deps`。

### Remote 配置

- `origin` → `https://github.com/Anyuluo996/MaiBot.git`（自己的 fork，推送目标）
- `upstream` → `https://github.com/MaiM-with-u/MaiBot.git`（官方仓库，拉取更新）

### 新建功能分支

```bash
git checkout main
git fetch upstream
git rebase upstream/main          # main 先对齐上游
git checkout -b feature/<功能名>  # 从对齐后的 main 切出
```

### 上游同步（定期 rebase）

当上游有更新时，在 feature 分支上执行：

```bash
git fetch upstream
git rebase upstream/main
# 解决冲突后：
git push origin feature/<功能名> --force-with-lease
```

冲突处理原则：
- 优先保留上游的实现，除非本 fork 的改动有明确理由
- 每个改动单元独立提交（见「提交规范」），rebase 冲突时可逐个 commit 处理
- `rebase --abort` 回退，不要用 `merge` 产生 merge commit 污染历史

---

## 提交规范

### Commit Message 格式

```
<type>(<scope>): <subject>

<body>
```

**type**（与上游一致）：
- `feat`：新功能
- `fix`：bug 修复
- `docs`：文档
- `test`：测试
- `chore`：构建/依赖/杂务
- `refactor`：重构（不改变行为）

**scope**（模块名）：
- `cache`：缓存优化相关
- `config`：配置系统
- `planner`：planner 阶段
- `replyer`：replyer 阶段
- `plugin`：插件系统
- `memory`：记忆系统
- `webui`：WebUI

**subject**：简体中文祈使句，不超过 50 字，结尾不加句号。

**body**（可选）：说明「为什么改」和「改了什么」，每行不超过 72 字。省略显而易见的解释。

### 示例

```
feat(cache): 滑动窗口稳定起点，根治 history 首条每轮变化

select_llm_context_messages 从 staticmethod 改为实例方法：
- __init__ 新增 _stable_window_start 状态字段
- planner 路径启用稳定窗口，子代理路径走原始无状态逻辑

效果：messages[1]（history 首条）跨轮次稳定，命中 Provider 隐式前缀缓存。
```

### 改动单元原则

- **一个改动单元 = 一个 commit**：一个 commit 只解决一个问题或实现一个独立功能
- **原子可逆**：每个 commit 单独可 revert，不影响其他 commit
- **测试和实现可以同 commit**：小改动的测试和实现放一个 commit；大改动拆成「实现 commit」+「测试 commit」
- **不混合无关改动**：不在同一个 commit 里改 bug + 加功能 + 格式化

---

## 代码风格

### 基本要求（遵循上游 AGENTS.md）

上游 `AGENTS.md` 已定义以下规范，本 fork 严格遵守：
- import 顺序（标准库 → 第三方 → 项目内部，按字母序）
- 注释规范（简体中文，说明「为什么」而非「是什么」）
- 类型注解规范（公开 API 必须有类型注解）
- 变量命名（有意义的名称，不用 `temp`/`data` 等模糊名）

### Ruff

项目已配置 `ruff`（见 `.pre-commit-config.yaml`）。提交前必须通过：

```bash
ruff check <修改的文件>
ruff format <修改的文件>   # 自动格式化
```

CI 会卡 `ruff.yml` / `ruff-pr.yml`，不通过会被拒绝。

### 语言

- 所有用户可见文本（日志、注释、WebUI、错误消息）默认简体中文
- commit message 用简体中文
- 变量名、函数名、类名用英文（遵循 Python 规范）
- prompt 模板改动必须同步中/英/日三语（上游硬约束）

### Fallback 禁忌

遵循上游约定：「不要总是考虑 fallback，如果哪里有错误，一定要让他及时完整的暴露」。不要用 try-except 兜底掩盖问题，除非有明确的降级语义（如缓存优化禁用时回退原始行为）。

---

## 开发流程

### 改动前

1. **确认范围**：改动涉及哪些模块？是否触碰 `src/A_memorix`（需先读 `MODIFICATION_POLICY.md`）、配置系统（需改模板+版本号）、prompt 模板（需同步三语）？
2. **从 main 切出分支**：`git checkout -b feature/<功能名>`
3. **核实代码**：不要猜测，读真实代码确认调用点、数据结构、字段名。计划阶段用 Explore agent 充分调研。

### 改动中

1. **遵循上游约束**：配置改动只改模板+版本号，不动 `legacy_migration`；不擅自加 `ConfigUpgradeHook`
2. **每个改动单元独立提交**：方便 rebase 和 cherry-pick
3. **写测试**：在 `pytests/<模块>/` 下加测试，验证改动行为

### 改动后

1. **静态检查**：
   ```bash
   ruff check <修改的文件>
   python -c "import ast; ast.parse(open('<文件>').read())"
   ```
2. **运行测试**：
   ```bash
   uv run pytest pytests/<模块>/     # 优先用 uv
   # 或
   python -m pytest pytests/<模块>/
   ```
3. **确认无回归**：受影响模块的现有测试必须通过
4. **提交并推送**：
   ```bash
   git add <文件>
   git commit -m "<type>(<scope>): <subject>"
   git push origin feature/<功能名>
   ```

### 环境注意事项

本仓库的测试依赖较重，本地环境可能缺失部分依赖（如 `babel`、`sqlmodel`）。遇到 `ModuleNotFoundError` 时：

```bash
pip install <缺失的包>        # 临时补
# 或
uv sync                       # 按 pyproject.toml 完整安装
```

`pyproject.toml` 和 `requirements.txt` 必须同步（上游硬约束）。

---

## CI 注意事项

上游有以下 CI workflow（`.github/workflows/`），fork 推送时会触发：

- `ruff.yml` / `ruff-pr.yml`：代码风格检查，**必须通过**
- `precheck.yml`：预检
- `i18n-validate.yml`：i18n 校验（prompt 改动必须同步三语）
- `a-memorix-tests.yml`：A_memorix 测试（改了记忆模块才会触发）

没有全仓库 pytest 的 CI，所以本地跑测试是主要保障。

fork 可以在 Settings → Actions 里关闭不需要的 CI（如 `docker-image-*`、`crowdin-*`），减少噪音。

---

## 本 Fork 的已知改动

记录 fork 相对上游的功能性差异，方便 rebase 时判断冲突优先级。

| 分支 | 改动 | 状态 | 影响文件 |
|---|---|---|---|
| `feature/cache-optimization` | 缓存优化（稳定窗口+时间精度+参考消息尾部+配置项） | 活跃 | `src/maisaka/chat_loop_service.py`, `src/maisaka/runtime.py`, `src/chat/replyer/maisaka_generator_base.py`, `src/config/official_configs.py`, `src/config/config.py` |

rebase 时如果上游也改了这些文件，需要逐文件评估：
- 上游的改动与本 fork 的改动无冲突 → 正常 rebase
- 上游重构了同一函数 → 优先保留上游结构，再把本 fork 的优化逻辑移植过去
- 上游已实现了类似功能 → 考虑废弃本 fork 的对应改动

---

## 参考资源

- 上游仓库：https://github.com/MaiM-with-u/MaiBot
- 上游开发约定：`AGENTS.md`（根目录）
- A_memorix 修改策略：`src/A_memorix/MODIFICATION_POLICY.md`
- 插件 SDK 文档：https://github.com/Mai-with-u/maibot-plugin-sdk/blob/main/docs/guide.md
- 插件开发指南（本 fork）：`docs/插件开发指南.md`
- 缓存优化分析：`docs/插件开发指南.md` 附录 + commit 历史
