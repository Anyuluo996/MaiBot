"""缓存优化功能测试。

覆盖四个改动单元：
1. 滑动窗口稳定起点（select_llm_context_messages 跨轮次起点稳定）
2. 时间精度降级（_build_time_user_message 按配置精度格式化）
3. 历史中间时间消息移除（_build_request_messages 跳过跨日时间消息）
4. ReferenceMessage 移到尾部（_build_request_messages 参考消息不在历史中段）
"""

from datetime import datetime, timedelta
from typing import List

import pytest

from src.maisaka.chat_loop_service import MaisakaChatLoopService
from src.maisaka.context.messages import (
    AssistantMessage,
    LLMContextMessage,
    ReferenceMessage,
)


def _make_assistant_messages(count: int, start_index: int = 0) -> List[LLMContextMessage]:
    """构造 count 条 AssistantMessage，时间按 start_index 递增（分钟级，避免溢出）。"""

    base_day = datetime(2026, 8, 7, 10, 0, 0)
    return [
        AssistantMessage(
            content=f"消息{start_index + i}",
            timestamp=base_day + timedelta(minutes=start_index + i),
        )
        for i in range(count)
    ]


class TestStableWindow:
    """滑动窗口稳定起点测试。"""

    def test_stable_window_start_persists_across_rounds(self) -> None:
        """连续两轮调用，history 增长后窗口起点索引应保持不变。"""

        service = MaisakaChatLoopService()
        max_ctx = 5  # effective = 5 * 2.0 = 10

        # 第一轮：20 条消息，effective=10，起点应为 index 10。
        history_round1 = _make_assistant_messages(20)
        selected1, _ = service.select_llm_context_messages(
            history_round1,
            request_kind="planner",
            max_context_size=max_ctx,
        )
        first_start = service._stable_window_start

        assert first_start is not None, "首轮应确定稳定窗口起点"
        assert len(selected1) == 10, f"首轮应选 10 条，实际 {len(selected1)}"

        # 第二轮：追加 2 条新消息（history 增长到 22 条），起点应不变。
        history_round2 = history_round1 + _make_assistant_messages(2, start_index=20)
        selected2, _ = service.select_llm_context_messages(
            history_round2,
            request_kind="planner",
            max_context_size=max_ctx,
        )

        assert service._stable_window_start == first_start, "第二轮起点应与首轮一致"
        assert len(selected2) == 12, f"第二轮应选 12 条（起点到尾部），实际 {len(selected2)}"

    def test_stable_window_advances_on_overflow(self) -> None:
        """窗口超过 2*effective_context_size 时起点应前移。"""

        service = MaisakaChatLoopService()
        max_ctx = 5  # effective = 10, 溢出阈值 = 20

        # 第一轮：20 条，起点 = 10。
        history_round1 = _make_assistant_messages(20)
        service.select_llm_context_messages(
            history_round1,
            request_kind="planner",
            max_context_size=max_ctx,
        )
        first_start = service._stable_window_start
        assert first_start == 10

        # 第二轮：追加 15 条（总计 35 条），从起点 10 到尾部有 25 条计数消息 > 20。
        # 起点应前移到使尾部 == 10 条，即 index 25。
        history_round2 = history_round1 + _make_assistant_messages(15, start_index=20)
        service.select_llm_context_messages(
            history_round2,
            request_kind="planner",
            max_context_size=max_ctx,
        )

        assert service._stable_window_start is not None
        assert service._stable_window_start > first_start, "溢出后起点应前移"

    def test_subagent_path_does_not_use_stable_window(self) -> None:
        """子代理路径(request_kind='sub_agent')不应启用稳定窗口。"""

        service = MaisakaChatLoopService()
        max_ctx = 5

        history = _make_assistant_messages(20)
        service.select_llm_context_messages(
            history,
            request_kind="sub_agent",
            max_context_size=max_ctx,
        )

        # 子代理路径不应设置稳定窗口起点。
        assert service._stable_window_start is None, "子代理路径不应启用稳定窗口"

    def test_disabled_when_config_off(self) -> None:
        """配置关闭 stable_window 时走原始无状态逻辑。"""

        from src.config.config import global_config

        service = MaisakaChatLoopService()
        max_ctx = 5

        original_stable = global_config.chat.cache_optimization.stable_window
        try:
            global_config.chat.cache_optimization.stable_window = False

            history = _make_assistant_messages(20)
            service.select_llm_context_messages(
                history,
                request_kind="planner",
                max_context_size=max_ctx,
            )

            assert service._stable_window_start is None, "stable_window=False 时不应启用"
        finally:
            global_config.chat.cache_optimization.stable_window = original_stable


class TestTimePrecision:
    """时间精度降级测试。"""

    @pytest.mark.parametrize(
        "precision,expected_format",
        [
            ("second", "%Y-%m-%d %H:%M:%S"),
            ("minute", "%Y-%m-%d %H:%M"),
            ("hour", "%Y-%m-%d %H:00"),
            ("day", "%Y-%m-%d"),
        ],
    )
    def test_time_precision_formats(self, precision: str, expected_format: str) -> None:
        """各精度应正确格式化时间戳。"""

        from src.config.config import global_config

        ts = datetime(2026, 8, 7, 14, 30, 45)
        expected = f"时间：{ts.strftime(expected_format)}"

        original_enabled = global_config.chat.cache_optimization.enabled
        original_precision = global_config.chat.cache_optimization.time_precision
        try:
            global_config.chat.cache_optimization.enabled = True
            global_config.chat.cache_optimization.time_precision = precision

            result = MaisakaChatLoopService._build_time_user_message(ts)
            assert result == expected, f"precision={precision}: got {result}, expected {expected}"
        finally:
            global_config.chat.cache_optimization.enabled = original_enabled
            global_config.chat.cache_optimization.time_precision = original_precision


class TestRemoveInlineTime:
    """历史中间时间消息移除测试。"""

    def test_inline_time_removed_when_enabled(self) -> None:
        """remove_inline_time 启用时，跨日时间消息不应出现在两条真实消息之间。"""

        from src.config.config import global_config

        history: List[LLMContextMessage] = [
            AssistantMessage(content="第一天消息", timestamp=datetime(2026, 8, 6, 23, 0, 0)),
            AssistantMessage(content="第二天消息", timestamp=datetime(2026, 8, 7, 9, 0, 0)),
        ]

        original = global_config.chat.cache_optimization.remove_inline_time
        try:
            global_config.chat.cache_optimization.remove_inline_time = True

            service = MaisakaChatLoopService(chat_system_prompt="sys")
            messages = service._build_request_messages(
                history,
                enable_visual_message=False,
                include_day_boundary_time_messages=True,
            )

            # 找到两条 AssistantMessage 的位置，检查它们之间不应有时间消息。
            from src.llm_models.payload_content.message import RoleType

            assistant_indices = [
                i for i, m in enumerate(messages) if m.role == RoleType.Assistant
            ]
            assert len(assistant_indices) == 2, f"应有 2 条 assistant 消息，实际 {len(assistant_indices)}"

            between_messages = messages[assistant_indices[0] + 1 : assistant_indices[1]]
            between_texts = [m.get_text_content() or "" for m in between_messages]
            inline_time = [t for t in between_texts if t.startswith("时间：")]
            assert not inline_time, f"两条真实消息之间不应有时间消息: {inline_time}"
        finally:
            global_config.chat.cache_optimization.remove_inline_time = original

    def test_inline_time_kept_when_disabled(self) -> None:
        """remove_inline_time 关闭时，跨日时间消息应保留在历史中间。"""

        from src.config.config import global_config

        history: List[LLMContextMessage] = [
            AssistantMessage(content="第一天消息", timestamp=datetime(2026, 8, 6, 23, 0, 0)),
            AssistantMessage(content="第二天消息", timestamp=datetime(2026, 8, 7, 9, 0, 0)),
        ]

        original_enabled = global_config.chat.cache_optimization.enabled
        original_remove = global_config.chat.cache_optimization.remove_inline_time
        try:
            global_config.chat.cache_optimization.enabled = True
            global_config.chat.cache_optimization.remove_inline_time = False

            service = MaisakaChatLoopService(chat_system_prompt="sys")
            messages = service._build_request_messages(
                history,
                enable_visual_message=False,
                include_day_boundary_time_messages=True,
            )

            history_texts = [
                m.get_text_content() for m in messages if m.get_text_content()
            ]
            inline_time = [t for t in history_texts if t.startswith("时间：")]
            assert len(inline_time) > 0, "关闭 remove_inline_time 时应有跨日时间消息"
        finally:
            global_config.chat.cache_optimization.enabled = original_enabled
            global_config.chat.cache_optimization.remove_inline_time = original_remove


class TestMoveReferencesToTail:
    """ReferenceMessage 移到尾部测试。"""

    def test_references_moved_to_tail(self) -> None:
        """move_references_to_tail 启用时，ReferenceMessage 应在真实对话之后。"""

        from src.config.config import global_config

        history: List[LLMContextMessage] = [
            AssistantMessage(content="真实消息1", timestamp=datetime(2026, 8, 7, 10, 0, 0)),
            ReferenceMessage(
                content="参考消息1",
                timestamp=datetime(2026, 8, 7, 10, 1, 0),
                remaining_uses_value=None,
            ),
            AssistantMessage(content="真实消息2", timestamp=datetime(2026, 8, 7, 10, 2, 0)),
        ]

        original_move = global_config.chat.cache_optimization.move_references_to_tail
        original_remove = global_config.chat.cache_optimization.remove_inline_time
        try:
            global_config.chat.cache_optimization.move_references_to_tail = True
            global_config.chat.cache_optimization.remove_inline_time = True

            service = MaisakaChatLoopService(chat_system_prompt="sys")
            messages = service._build_request_messages(
                history,
                enable_visual_message=False,
            )

            texts = [m.get_text_content() or "" for m in messages]
            ref_indices = [i for i, t in enumerate(texts) if "[参考消息]" in t]
            real_indices = [i for i, t in enumerate(texts) if "真实消息" in t]

            assert ref_indices, "应有参考消息"
            assert real_indices, "应有真实消息"

            max_real = max(real_indices)
            min_ref = min(ref_indices)
            assert max_real < min_ref, (
                f"真实消息(max_idx={max_real})应在参考消息(min_idx={min_ref})之前"
            )
        finally:
            global_config.chat.cache_optimization.move_references_to_tail = original_move
            global_config.chat.cache_optimization.remove_inline_time = original_remove
