"""Maisaka Planner 跨日时间提示测试。

本测试验证的是「缓存优化关闭时」的原始跨日时间消息行为。
缓存优化（remove_inline_time）默认开启时会移除这些消息，
所以测试需要显式关闭该选项才能验证旧行为。
"""

from datetime import datetime
from typing import List

import pytest

from src.config.config import global_config
from src.llm_models.payload_content.message import Message, RoleType
from src.llm_models.payload_content.tool_option import ToolCall
from src.maisaka.chat_loop_service import MaisakaChatLoopService
from src.maisaka.context.messages import AssistantMessage, LLMContextMessage, ReferenceMessage, ToolResultMessage


@pytest.fixture(autouse=True)
def _disable_cache_optimization_for_legacy_behavior():
    """临时关闭缓存优化，让本测试验证原始跨日时间消息和参考消息行为。"""

    original_remove = global_config.chat.cache_optimization.remove_inline_time
    original_move = global_config.chat.cache_optimization.move_references_to_tail
    original_precision = global_config.chat.cache_optimization.time_precision
    global_config.chat.cache_optimization.remove_inline_time = False
    global_config.chat.cache_optimization.move_references_to_tail = False
    global_config.chat.cache_optimization.time_precision = "second"
    yield
    global_config.chat.cache_optimization.remove_inline_time = original_remove
    global_config.chat.cache_optimization.move_references_to_tail = original_move
    global_config.chat.cache_optimization.time_precision = original_precision


def _build_history_messages(history: List[LLMContextMessage]) -> List[Message]:
    """构造请求并移除固定的 system 与末尾当前时间消息。"""

    service = MaisakaChatLoopService(chat_system_prompt="system")
    messages = service._build_request_messages(
        history,
        enable_visual_message=False,
        include_day_boundary_time_messages=True,
    )
    return messages[1:-1]


def test_day_boundary_is_deferred_until_after_tool_result() -> None:
    history: List[LLMContextMessage] = [
        AssistantMessage(
            content="调用表情工具",
            timestamp=datetime(2026, 7, 20, 23, 59, 59),
            tool_calls=[ToolCall(call_id="call_emoji", func_name="send_emoji", args={})],
        ),
        ToolResultMessage(
            content="表情包发送成功",
            timestamp=datetime(2026, 7, 21, 0, 0, 1),
            tool_call_id="call_emoji",
            tool_name="send_emoji",
        ),
        ReferenceMessage(
            content="工具后的普通消息",
            timestamp=datetime(2026, 7, 21, 0, 0, 2),
            remaining_uses_value=None,
        ),
    ]

    messages = _build_history_messages(history)

    assert [message.role for message in messages] == [
        RoleType.Assistant,
        RoleType.Tool,
        RoleType.User,
        RoleType.User,
    ]
    assert messages[1].tool_call_id == "call_emoji"
    assert messages[2].get_text_content() == "时间：2026-07-21 00:00:01"
    assert messages[3].get_text_content() == "[参考消息]\n工具后的普通消息"


def test_day_boundary_is_deferred_until_after_all_tool_results() -> None:
    history: List[LLMContextMessage] = [
        AssistantMessage(
            content="调用多个工具",
            timestamp=datetime(2026, 7, 20, 23, 59, 59),
            tool_calls=[
                ToolCall(call_id="call_first", func_name="first_tool", args={}),
                ToolCall(call_id="call_second", func_name="second_tool", args={}),
            ],
        ),
        ToolResultMessage(
            content="第一个工具执行成功",
            timestamp=datetime(2026, 7, 20, 23, 59, 59, 500000),
            tool_call_id="call_first",
            tool_name="first_tool",
        ),
        ToolResultMessage(
            content="第二个工具执行成功",
            timestamp=datetime(2026, 7, 21, 0, 0, 1),
            tool_call_id="call_second",
            tool_name="second_tool",
        ),
    ]

    messages = _build_history_messages(history)

    assert [message.role for message in messages] == [
        RoleType.Assistant,
        RoleType.Tool,
        RoleType.Tool,
        RoleType.User,
    ]
    assert [message.tool_call_id for message in messages[1:3]] == ["call_first", "call_second"]
    assert messages[3].get_text_content() == "时间：2026-07-21 00:00:01"


def test_day_boundary_stays_before_regular_context_message() -> None:
    history: List[LLMContextMessage] = [
        ReferenceMessage(
            content="跨日前消息",
            timestamp=datetime(2026, 7, 20, 23, 59, 59),
            remaining_uses_value=None,
        ),
        ReferenceMessage(
            content="跨日后消息",
            timestamp=datetime(2026, 7, 21, 0, 0, 1),
            remaining_uses_value=None,
        ),
    ]

    messages = _build_history_messages(history)

    assert [message.role for message in messages] == [RoleType.User, RoleType.User, RoleType.User]
    assert messages[1].get_text_content() == "时间：2026-07-21 00:00:01"
    assert messages[2].get_text_content() == "[参考消息]\n跨日后消息"
