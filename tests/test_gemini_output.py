"""normalize_gemini_reply 思考块剥离测试。

覆盖 `<thought>` 标签（wo4 出站泄漏形态）与既有四形态的回归；
另验证 `_build_replyer_output_instruction` 输出宪法关键词。
"""

import pytest

from src.chat.replyer.gemini_output import GeminiReplyFormatError, normalize_gemini_reply


def test_thought_tag_paired_stripped():
    assert normalize_gemini_reply("<thought>分析一下场面</thought>这是回复") == "这是回复"


def test_thought_nested_inside_thinking():
    assert normalize_gemini_reply("<thinking>外层<thought>内层</thought>还在想</thinking>正文") == "正文"


def test_thought_unclosed_raises():
    with pytest.raises(GeminiReplyFormatError):
        normalize_gemini_reply("<thought>分析没写完，直接跟了正文")


def test_thought_mismatched_tag_raises():
    with pytest.raises(GeminiReplyFormatError):
        normalize_gemini_reply("<thought>用错闭合标签</thinking>")


def test_thought_only_output_raises():
    with pytest.raises(GeminiReplyFormatError):
        normalize_gemini_reply("<thought>只有思考</thought>")


def test_thinking_tag_regression():
    assert normalize_gemini_reply("<thinking>inner</thinking>正文") == "正文"


def test_angle_bracket_regression():
    assert normalize_gemini_reply("⌊思考过程:\n逐条分析场面\n⌈你好呀") == "你好呀"


def test_bracket_thought_regression():
    assert normalize_gemini_reply("[:thought\n草稿\n]\n正文内容") == "正文内容"


def test_leading_plain_header_still_blocked():
    with pytest.raises(GeminiReplyFormatError):
        normalize_gemini_reply("思考过程：先看看谁在说话\n然后正文消失了")


def test_mid_text_thought_not_stripped():
    """只剥离开头有明确边界的思考块；正文中间提到思考不触发（既有设计）。"""
    text = "前面正文<thought>x</thought>后面正文"
    assert normalize_gemini_reply(text) == text


def test_plain_text_untouched():
    text = "我刚才的那个 thought 其实是想说，今天天气不错。"
    assert normalize_gemini_reply(text) == text


def test_output_instruction_contains_constitution():
    """五 locale 分支都包含 Markdown 禁用关键词（词形跨语言一致）。"""
    from src.chat.replyer.maisaka_generator_base import BaseMaisakaReplyGenerator

    instruction = BaseMaisakaReplyGenerator._build_replyer_output_instruction()
    assert "Markdown" in instruction
