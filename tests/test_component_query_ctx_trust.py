"""宿主工具调用上下文收窄补丁的单元测试（patches/history-search-tool-context-override.patch）。

覆盖设计方案 §3.1 的四个必备用例，外加两条 fail-closed 前提：
  1. 声明键不覆盖（模型值保留）
  2. 未声明键覆盖（宿主值强制写入）
  3. ``_ctx_trusted`` 恒写入且与 HMAC(secret, stream_id) 一致
  4. 原始 schema（parameters_raw.properties）声明的 user_id 不被覆盖
  5. HOST_TRUST_SECRET 未注入 → 不写令牌（fail-closed 前提）
  6. 旧 Action 执行器路径（tool_entry=None）行为不变
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.core.tooling import ToolExecutionContext, ToolInvocation
from src.plugin_runtime.component_query import (
    _HOST_TRUST_SECRET_ENV,
    ComponentQueryService,
    _trust_token,
)


@dataclass
class _FakeToolEntry:
    """只保留补丁读取的两个字段的最小工具条目替身。"""

    parameters: List[Dict[str, Any]] = field(default_factory=list)
    parameters_raw: Any = field(default_factory=dict)


def _make_context(
    stream_id: str = "session-abc-123",
    group_id: str = "group-1",
    user_id: str = "user-real",
    platform: str = "qq",
) -> ToolExecutionContext:
    """构造带完整会话字段的工具执行上下文。"""

    return ToolExecutionContext(
        session_id=stream_id,
        stream_id=stream_id,
        group_id=group_id,
        user_id=user_id,
        platform=platform,
        is_group_chat=True,
    )


def _run(invocation_args: Dict[str, Any], entry: Optional[_FakeToolEntry]) -> Dict[str, Any]:
    """以固定上下文执行被测函数，返回发往 Runner 的参数字典。"""

    invocation = ToolInvocation("some_tool", arguments=dict(invocation_args))
    return ComponentQueryService._build_tool_invocation_payload(
        invocation,
        _make_context(),
        tool_entry=entry,
    )


def test_declared_key_not_overridden(monkeypatch) -> None:
    """工具显式声明 user_id 时，模型传入的值保留，宿主值不覆盖。"""

    monkeypatch.setenv(_HOST_TRUST_SECRET_ENV, "test-secret")
    entry = _FakeToolEntry(
        parameters=[{"name": "user_id"}, {"name": "query"}],
        parameters_raw={},
    )
    payload = _run({"query": "hello", "user_id": "user-model-chose"}, entry)
    assert payload["user_id"] == "user-model-chose"
    # 未声明的保留键仍被覆盖
    assert payload["stream_id"] == "session-abc-123"
    assert payload["group_id"] == "group-1"


def test_undeclared_key_overridden(monkeypatch) -> None:
    """工具未声明 stream_id 时，模型自带的伪造值被宿主值强制覆盖。"""

    monkeypatch.setenv(_HOST_TRUST_SECRET_ENV, "test-secret")
    entry = _FakeToolEntry(
        parameters=[{"name": "query"}],
        parameters_raw={},
    )
    payload = _run(
        {"query": "hello", "stream_id": "forged-session", "chat_id": "forged",
         "_ctx_trusted": "model-forged-token"},
        entry,
    )
    assert payload["stream_id"] == "session-abc-123"
    assert payload["chat_id"] == "session-abc-123"
    assert payload["platform"] == "qq"
    # 模型自带的 _ctx_trusted 一并被真实令牌覆盖（不残留伪造值）
    assert payload["_ctx_trusted"] == hmac.new(
        b"test-secret", b"session-abc-123", hashlib.sha256
    ).hexdigest()


def test_ctx_trusted_written(monkeypatch) -> None:
    """``_ctx_trusted`` 恒写入，且等于 HMAC-SHA256(secret, 最终 stream_id)。"""

    monkeypatch.setenv(_HOST_TRUST_SECRET_ENV, "test-secret")
    entry = _FakeToolEntry(parameters=[{"name": "query"}], parameters_raw={})
    payload = _run({"query": "hello"}, entry)
    expected = hmac.new(
        b"test-secret", b"session-abc-123", hashlib.sha256
    ).hexdigest()
    assert payload["_ctx_trusted"] == expected
    # 与独立实现的 _trust_token 一致
    assert payload["_ctx_trusted"] == _trust_token("session-abc-123")


def test_raw_schema_declared_user_id_not_overridden(monkeypatch) -> None:
    """原始 schema（parameters_raw.properties）声明的 user_id 同样不被覆盖。"""

    monkeypatch.setenv(_HOST_TRUST_SECRET_ENV, "test-secret")
    entry = _FakeToolEntry(
        parameters=[],
        parameters_raw={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "user_id": {"type": "string", "description": "禁言目标"},
            },
        },
    )
    payload = _run({"query": "hello", "user_id": "12345"}, entry)
    assert payload["user_id"] == "12345"


def test_missing_secret_no_token(monkeypatch) -> None:
    """HOST_TRUST_SECRET 未注入时不写令牌；插件侧缺令牌 fail-closed（补丁未生效语义）。"""

    monkeypatch.delenv(_HOST_TRUST_SECRET_ENV, raising=False)
    entry = _FakeToolEntry(parameters=[{"name": "query"}], parameters_raw={})
    payload = _run({"query": "hello"}, entry)
    assert "_ctx_trusted" not in payload


def test_legacy_executor_path_unchanged(monkeypatch) -> None:
    """旧 Action 执行器路径不传 tool_entry：行为与补丁前一致，不注入令牌。"""

    monkeypatch.setenv(_HOST_TRUST_SECRET_ENV, "test-secret")
    invocation = ToolInvocation("legacy_action", arguments={"target": "x"})
    payload = ComponentQueryService._build_tool_invocation_payload(
        invocation, None, legacy_action=True
    )
    assert "_ctx_trusted" not in payload
    # legacy_action 语义保留：stream_id/chat_id/reasoning/action_data 补齐
    assert payload["stream_id"] == ""
    assert payload["action_data"] == {"target": "x"}
