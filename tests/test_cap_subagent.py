"""宿主子代理能力测试：参数校验 / 会话定位 / 全局并发护栏 / 注册表清理 /
status-stop 跨会话路由。内置工具执行循环（handle_tool）打桩，桥接逻辑全真。
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from src.maisaka.subagent_tasks import SubAgentOutcome, SubAgentTask, SubAgentTaskManager
from src.plugin_runtime.capabilities import subagent as cap_subagent


class FakeLogger:
    def info(self, *args: Any) -> None:
        pass

    def warning(self, *args: Any) -> None:
        pass

    def error(self, *args: Any) -> None:
        pass


class FakeTaskManager(SubAgentTaskManager):
    """真实 manager + 可控假 runner（不经 LLM，直接产出结论）。"""

    def __init__(self) -> None:
        super().__init__()
        self.conclusion_text = "结论内容"

    async def _fake_runner(self, task: SubAgentTask) -> Any:
        return SubAgentOutcome(conclusion=self.conclusion_text, tool_usage={})


class FakeRuntime:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.log_prefix = f"[{session_id}]"
        self._tool_registry: Any = SimpleNamespace()
        self._subagent_tasks = FakeTaskManager()
        self._reasoning_engine = SimpleNamespace()
        self.deliver_calls: List[Any] = []

    def deliver_subagent_result(self, task: SubAgentTask) -> None:
        self.deliver_calls.append(task)

    async def run_sub_agent(self, **kwargs: Any) -> Any:
        # 无 tool_calls 的收尾轮：子代理循环第一轮即返回结论
        return SimpleNamespace(content="done", tool_calls=[], raw_messages=[])


@pytest.fixture()
def fake_heartflow(monkeypatch: pytest.MonkeyPatch) -> Dict[str, FakeRuntime]:
    """伪造 heartflow_manager 的会话表（handler 在调用点延迟导入）。"""
    import src.chat.heart_flow.heartflow_manager as hf_module

    runtimes: Dict[str, FakeRuntime] = {
        "s_group_1": FakeRuntime("s_group_1"),
        "s_group_2": FakeRuntime("s_group_2"),
    }
    monkeypatch.setattr(hf_module, "heartflow_manager", SimpleNamespace(heartflow_chat_list=runtimes))
    return runtimes


def _clean_registry() -> None:
    cap_subagent._task_registry.clear()


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_spawn_param_validation(fake_heartflow: Any) -> None:
    _clean_registry()
    for args in ({}, {"stream_id": "s_group_1"}, {"stream_id": "s_group_1", "description": "x"}):
        result = _run(cap_subagent.spawn("anyul.test", "maisaka.subagent.spawn", args))
        assert result["success"] is False and result.get("error")


def test_spawn_session_not_found(fake_heartflow: Any) -> None:
    _clean_registry()
    result = _run(
        cap_subagent.spawn(
            "anyul.test", "maisaka.subagent.spawn",
            {"stream_id": "s_missing", "description": "查资料", "granted_tools": ["web_search"]},
        )
    )
    assert result["success"] is False and result["reason"] == "session_not_found"


def test_spawn_tool_registry_not_ready(fake_heartflow: Any) -> None:
    _clean_registry()
    fake_heartflow["s_group_1"]._tool_registry = None
    result = _run(
        cap_subagent.spawn(
            "anyul.test", "maisaka.subagent.spawn",
            {"stream_id": "s_group_1", "description": "查资料", "granted_tools": ["web_search"]},
        )
    )
    assert result["success"] is False and "注册表" in result["error"]


def test_spawn_delegates_and_registers(fake_heartflow: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _clean_registry()
    cap_subagent._ensure_imports()  # 先完成真实加载，再整体替换桩
    captured: Dict[str, Any] = {}

    async def fake_handle_tool(tool_ctx: Any, invocation: Any) -> Any:
        captured["tool_ctx"] = tool_ctx
        captured["invocation"] = invocation
        return SimpleNamespace(
            success=True,
            content="子代理已在后台开始工作",
            structured_content={"task_id": "subagent-1", "granted_tools": ["web_search"], "rejected_tools": []},
            error_message="",
        )

    monkeypatch.setattr(cap_subagent, "_handle_tool", fake_handle_tool)
    result = _run(
        cap_subagent.spawn(
            "anyul.task-loop", "maisaka.subagent.spawn",
            {"stream_id": "s_group_1", "description": "并行查证", "granted_tools": ["web_search"]},
        )
    )
    assert result["success"] is True and result["task_id"] == "subagent-1"
    assert cap_subagent._task_registry == {"subagent-1": "s_group_1"}
    # 桥接把 runtime/engine 组装进 tool_ctx；invocation 携带调用方插件 id
    assert captured["tool_ctx"].runtime.session_id == "s_group_1"
    assert captured["invocation"].metadata["plugin_id"] == "anyul.task-loop"
    assert captured["invocation"].arguments["task"] == "并行查证"


def test_global_cap_refuses_when_full(fake_heartflow: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _clean_registry()
    cap_subagent._ensure_imports()

    async def fake_handle_tool(tool_ctx: Any, invocation: Any) -> Any:
        task_id = f"subagent-{time.time_ns()}"
        # 与真实 handle_tool 对齐：任务登记进 manager（注册表 prune 依据）
        tool_ctx.runtime._subagent_tasks._tasks[task_id] = SubAgentTask(
            task_id=task_id, description="x", granted_tools=[]
        )
        return SimpleNamespace(
            success=True, content="ok",
            structured_content={"task_id": task_id, "granted_tools": [], "rejected_tools": []},
            error_message="",
        )

    monkeypatch.setattr(cap_subagent, "_handle_tool", fake_handle_tool)
    monkeypatch.setattr(cap_subagent, "_GLOBAL_TASK_LIMIT", 2)
    for i in range(2):
        result = _run(
            cap_subagent.spawn("anyul.test", "maisaka.subagent.spawn",
                               {"stream_id": "s_group_1", "description": f"任务{i}", "granted_tools": ["web_search"]})
        )
        assert result["success"] is True
    result = _run(
        cap_subagent.spawn("anyul.test", "maisaka.subagent.spawn",
                           {"stream_id": "s_group_2", "description": "第三个", "granted_tools": ["web_search"]})
    )
    assert result["success"] is False and result["reason"] == "global_max_running"


def test_status_routes_via_registry(fake_heartflow: Any) -> None:
    _clean_registry()
    cap_subagent._ensure_imports()
    task = SubAgentTask(task_id="subagent-9", description="查资料", granted_tools=["web_search"])
    fake_heartflow["s_group_2"]._subagent_tasks._tasks["subagent-9"] = task
    cap_subagent._task_registry["subagent-9"] = "s_group_2"
    result = _run(cap_subagent.status("anyul.test", "maisaka.subagent.status", {"task_id": "subagent-9"}))
    assert result["success"] is True
    missing = _run(cap_subagent.status("anyul.test", "maisaka.subagent.status", {"task_id": "subagent-none"}))
    assert missing["success"] is False
    aggregate = _run(cap_subagent.status("anyul.test", "maisaka.subagent.status", {}))
    assert aggregate["success"] is True
    assert any(task["task_id"] == "subagent-9" for task in aggregate["tasks"])


def test_stop_unknown_task_honest_failure(fake_heartflow: Any) -> None:
    _clean_registry()
    result = _run(cap_subagent.stop("anyul.test", "maisaka.subagent.stop", {"task_id": "subagent-none"}))
    assert result["success"] is False


def test_prune_registry_drops_finished(fake_heartflow: Any) -> None:
    _clean_registry()
    cap_subagent._task_registry["dead"] = "s_group_1"  # manager 里没有该任务
    cap_subagent._prune_registry()
    assert "dead" not in cap_subagent._task_registry
