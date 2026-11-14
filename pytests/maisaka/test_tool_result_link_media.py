"""链接注册媒体在 maisaka 侧的媒体消息、索引注解与引用解析测试。"""

from base64 import b64encode
from datetime import datetime
from io import BytesIO
from unittest.mock import Mock

import pytest
from PIL import Image

from src.common.data_models.message_component_data_model import ImageComponent, MessageSequence, TextComponent
from src.core.tooling import ToolContentItem, ToolExecutionResult
from src.llm_models.payload_content.tool_option import ToolCall
from src.maisaka.context.image_attachment import resolve_context_image
from src.maisaka.context.messages import ToolResultMediaMessage
from src.maisaka.reasoning_engine import MaisakaReasoningEngine
from src.maisaka.runtime import MaisakaHeartFlowChatting

SOURCE_URL = "http://10.126.126.2:8765/hwimg/lan/d523.png"


def png_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (2, 2), "green").save(output, format="PNG")
    return output.getvalue()


def image_component() -> ImageComponent:
    return ImageComponent(binary_hash="", binary_data=png_bytes())


def resolve_runtime() -> MaisakaHeartFlowChatting:
    chat = object.__new__(MaisakaHeartFlowChatting)
    chat._chat_history = []
    return chat


def media_engine() -> MaisakaReasoningEngine:
    engine = object.__new__(MaisakaReasoningEngine)
    engine._runtime = Mock()
    engine._runtime.session_id = "sess"
    return engine


def enriched_result() -> ToolExecutionResult:
    """模拟 link_media enrichment 后的工具结果：原 text 项 + 注入的 image 项。"""

    result = ToolExecutionResult(
        tool_name="send_daily_info_pic", success=True,
        content=f"已执行。图片链接：{SOURCE_URL}",
    )
    result.content_items.append(ToolContentItem(content_type="text", text=result.content))
    result.content_items.append(ToolContentItem(
        content_type="image", data=b64encode(png_bytes()).decode("ascii"), mime_type="image/png",
        metadata={"source_url": SOURCE_URL},
    ))
    return result


def test_enriched_result_builds_media_message_and_annotation():
    engine = media_engine()
    tool_call = ToolCall(call_id="call_0ff503eb", func_name="send_daily_info_pic")
    result = enriched_result()

    messages = engine._build_tool_result_media_messages(tool_call, result)

    assert len(messages) == 1
    message = messages[0]
    # 注入项位于原 text 项之后，媒体索引按 content_items 位次（1 起）编号
    assert message.message_id == "tool_result:call_0ff503eb:2"
    assert message.tool_name == "send_daily_info_pic"
    images = [component for component in message.raw_message.components if isinstance(component, ImageComponent)]
    assert images and images[0].binary_data == png_bytes()

    annotation = engine._build_tool_result_history_content(tool_call, result)
    assert '<tool_result_media_list>' in annotation
    assert 'msg_id="tool_result:call_0ff503eb:2"' in annotation
    assert f'source_url="{SOURCE_URL}"' in annotation


@pytest.mark.asyncio
async def test_enriched_tool_media_resolves_via_exact_and_tool_name_index():
    chat = resolve_runtime()
    image = image_component()
    chat._chat_history.append(ToolResultMediaMessage(
        tool_call_id="call_0ff503eb", tool_name="send_daily_info_pic",
        raw_message=MessageSequence([TextComponent("<tool_result_media />"), image]),
        visible_text="<tool_result_media msg_id=\"tool_result:call_0ff503eb:2\" /> [图片]",
        timestamp=datetime.now(), message_id="tool_result:call_0ff503eb:2",
    ))

    exact = await resolve_context_image(chat, "tool_result:call_0ff503eb:2")
    assert exact.binary_data == png_bytes()

    # 模型用工具名组索引时按最近一次同名工具媒体宽容解析
    tolerant = await resolve_context_image(chat, "tool_result:send_daily_info_pic:0")
    assert tolerant.binary_data == png_bytes()

    with pytest.raises(ValueError, match="没有找到工具媒体"):
        await resolve_context_image(chat, "tool_result:get_user_wuwa_char_detail:1")


@pytest.mark.asyncio
async def test_fabricated_index_still_reports_verbatim_without_matching_media():
    chat = resolve_runtime()
    fabricated = "tool_result:bba463584adb453aac2ed7898a1564ae:1"
    with pytest.raises(ValueError) as error:
        await resolve_context_image(chat, fabricated)
    assert fabricated in str(error.value)
    assert "没有找到工具媒体" in str(error.value)
