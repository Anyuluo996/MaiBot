"""宿主子代理能力：把后台子代理机制暴露为插件可用能力。

桥接层：按 stream_id 定位会话 runtime（heartflow_manager），复用内置
subagent 工具的完整执行机制（授权校验 / 每会话并发上限 / 观察事件 /
结论回投唤醒 Planner），本模块只做会话定位与全局并发护栏——机制零改动。

  - maisaka.subagent.spawn：立即返回 task_id（结论走内置回投通道；
    RPC 60s 超时约束下禁止任何长阻塞调用）
  - maisaka.subagent.status：单任务详情或跨会话聚合列表
  - maisaka.subagent.stop：终止在途任务

全局在途上限（_GLOBAL_TASK_LIMIT）在每会话上限（SUBAGENT_MAX_RUNNING_TASKS）
之外防多群并发风暴；注册表惰性清理（按任务实际状态修剪）。
"""

from typing import Any, Dict, List, Optional

import time
import uuid

from src.common.logger import get_logger

logger = get_logger("cap_subagent")

# 全局在途任务上限（跨会话软上限）
_GLOBAL_TASK_LIMIT = 8

# 全局在途注册表：task_id -> session_id（插入序）
_task_registry: Dict[str, str] = {}

# 内置工具机制惰性加载（builtin_tool ↔ runtime 有既有的导入顺序依赖，
# 顶层导入会经 hook_catalog/registry 反向成环；首用时一次性载入。
# 独立 _loaded 旗标：测试可整体替换 _handle_* 桩而不被重载覆盖）
_handle_tool: Any = None
_handle_status_tool: Any = None
_handle_stop_tool: Any = None
_builtin_tool_ctx_cls: Any = None
_tool_invocation_cls: Any = None
_subagent_tool_name: str = "subagent"
_loaded: bool = False


def _ensure_imports() -> None:
    global _handle_tool, _handle_status_tool, _handle_stop_tool, _builtin_tool_ctx_cls, _tool_invocation_cls, _loaded
    global _subagent_tool_name
    if _loaded:
        return
    from src.core.tooling import ToolInvocation
    from src.maisaka.builtin_tool.context import BuiltinToolRuntimeContext
    from src.maisaka.builtin_tool.subagent import (
        SUBAGENT_TOOL_NAME,
        handle_status_tool,
        handle_stop_tool,
        handle_tool,
    )

    _handle_tool = handle_tool
    _handle_status_tool = handle_status_tool
    _handle_stop_tool = handle_stop_tool
    _builtin_tool_ctx_cls = BuiltinToolRuntimeContext
    _tool_invocation_cls = ToolInvocation
    _subagent_tool_name = SUBAGENT_TOOL_NAME
    _loaded = True


def _lookup_runtime(session_id: str) -> Optional[Any]:
    """按 session_id 取会话 runtime；未激活返回 None。"""

    from src.chat.heart_flow.heartflow_manager import heartflow_manager

    return heartflow_manager.heartflow_chat_list.get(session_id)


def _prune_registry() -> None:
    """按任务实际状态修剪注册表（结束/不存在即移除）。"""

    for task_id in list(_task_registry.keys()):
        runtime = _lookup_runtime(_task_registry[task_id])
        if runtime is None:
            del _task_registry[task_id]
            continue
        task = runtime._subagent_tasks.get(task_id)
        if task is None or task.status != "running":
            del _task_registry[task_id]


def _active_global_count() -> int:
    _prune_registry()
    return len(_task_registry)


def _build_tool_ctx(runtime: Any) -> Any:
    _ensure_imports()
    return _builtin_tool_ctx_cls(runtime._reasoning_engine, runtime)


async def spawn(plugin_id: str, capability: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """派发一个后台子代理；立即返回 task_id，结论经内置回投通道送回 Planner。"""

    del capability
    stream_id = str(args.get("stream_id") or "").strip()
    description = str(args.get("description") or args.get("task") or "").strip()
    granted_tools = args.get("granted_tools")
    if not stream_id:
        return {"success": False, "error": "缺少必要参数 stream_id"}
    if not description:
        return {"success": False, "error": "缺少必要参数 description"}
    if not isinstance(granted_tools, list) or not granted_tools:
        return {"success": False, "error": "granted_tools 需为非空工具名列表"}
    if _active_global_count() >= _GLOBAL_TASK_LIMIT:
        return {"success": False, "reason": "global_max_running", "error": f"全局在途子代理已达 {_GLOBAL_TASK_LIMIT} 个"}
    runtime = _lookup_runtime(stream_id)
    if runtime is None:
        return {"success": False, "reason": "session_not_found", "error": "会话不存在或未激活"}
    if runtime._tool_registry is None:
        return {"success": False, "error": "统一工具注册表尚未初始化"}

    _ensure_imports()
    tool_ctx = _build_tool_ctx(runtime)
    invocation = _tool_invocation_cls(
        tool_name=_subagent_tool_name,
        arguments={"task": description, "tools": [str(name).strip() for name in granted_tools]},
        call_id=f"cap_{uuid.uuid4().hex[:12]}",
        session_id=stream_id,
        stream_id=stream_id,
        metadata={"source": "plugin_capability", "plugin_id": plugin_id},
    )
    result = await _handle_tool(tool_ctx, invocation)
    structured = result.structured_content if isinstance(result.structured_content, dict) else {}
    task_id = str(structured.get("task_id") or "")
    if result.success and task_id:
        _task_registry[task_id] = stream_id
        logger.info(
            "[cap.subagent] 插件 %s 派发子代理 task_id=%s session=%s granted=%s",
            plugin_id, task_id, stream_id, structured.get("granted_tools"),
        )
    return {
        "success": result.success,
        "task_id": task_id,
        "granted_tools": structured.get("granted_tools", []),
        "rejected_tools": structured.get("rejected_tools", []),
        "message": result.content or "",
        "error_message": result.error_message or "",
    }


async def status(plugin_id: str, capability: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """单任务详情（跨会话路由）或全部在途任务聚合列表。"""

    del capability
    _prune_registry()
    task_id = str(args.get("task_id") or "").strip()
    if task_id:
        session_id = _task_registry.get(task_id)
        if session_id is None:
            return {"success": False, "error": f"没有 id 为 {task_id} 的子代理任务（或已结束被清理）"}
        runtime = _lookup_runtime(session_id)
        if runtime is None:
            return {"success": False, "error": "任务所属会话已下线"}
        _ensure_imports()
        result = await _handle_status_tool(
            _build_tool_ctx(runtime),
            _tool_invocation_cls(tool_name="subagent_status", arguments={"task_id": task_id}, session_id=session_id),
        )
        return {
            "success": result.success,
            "message": result.content or "",
            "task": result.structured_content or {},
            "error_message": result.error_message or "",
        }

    tasks: List[Dict[str, Any]] = []
    seen_sessions: set = set()
    for session_id in dict.fromkeys(_task_registry.values()):
        runtime = _lookup_runtime(session_id)
        if runtime is None or id(runtime) in seen_sessions:
            continue
        seen_sessions.add(id(runtime))
        for task in runtime._subagent_tasks.list_tasks():
            tasks.append({"task_id": task.task_id, "session_id": session_id, "status": task.status})
    return {"success": True, "tasks": tasks}


async def stop(plugin_id: str, capability: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """终止一个在途任务；任务不存在或已结束时如实返回失败。"""

    del capability
    task_id = str(args.get("task_id") or "").strip()
    if not task_id:
        return {"success": False, "error": "缺少必要参数 task_id"}
    _prune_registry()
    session_id = _task_registry.get(task_id)
    if session_id is None:
        return {"success": False, "error": f"没有 id 为 {task_id} 的子代理任务（或已结束被清理）"}
    runtime = _lookup_runtime(session_id)
    if runtime is None:
        return {"success": False, "error": "任务所属会话已下线"}
    _ensure_imports()
    result = await _handle_stop_tool(
        _build_tool_ctx(runtime),
        _tool_invocation_cls(tool_name="subagent_stop", arguments={"task_id": task_id}, session_id=session_id),
    )
    return {"success": result.success, "message": result.content or "", "error_message": result.error_message or ""}
