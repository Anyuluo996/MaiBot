"""链接注册媒体（工具结果图片链接 → image 内容项）单元测试。"""

from base64 import b64decode
from io import BytesIO
from unittest.mock import Mock

import pytest
from PIL import Image

from src.core.tooling import ToolContentItem, ToolExecutionResult
from src.mcp_module import link_media


def png_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (2, 2), "red").save(output, format="PNG")
    return output.getvalue()


def text_result(content: str, *, success: bool = True) -> ToolExecutionResult:
    result = ToolExecutionResult(tool_name="send_daily_info_pic", success=success, content=content)
    result.content_items.append(ToolContentItem(content_type="text", text=content))
    return result


@pytest.mark.asyncio
async def test_enrich_injects_image_item_from_text_link(monkeypatch):
    payload = png_bytes()

    async def fake_download(url: str) -> bytes:
        return payload

    monkeypatch.setattr(link_media, "_download_image_bytes", fake_download)
    result = text_result("已执行，但没有产出可发送的文本或图片。\n图片链接：http://10.126.126.2:8765/hwimg/lan/d523.png。")

    registered = await link_media.enrich_tool_result_with_link_images(result)

    assert registered == 1
    assert len(result.content_items) == 2
    item = result.content_items[-1]
    assert item.content_type == "image"
    assert item.mime_type == "image/png"
    # 尾部句号不进入注册的 source_url
    assert item.metadata["source_url"] == "http://10.126.126.2:8765/hwimg/lan/d523.png"
    assert b64decode(item.data) == payload


@pytest.mark.asyncio
async def test_download_failure_and_non_image_payload_are_skipped(monkeypatch):
    responses = {"http://10.126.126.2:8765/a.png": b"", "http://10.126.126.2:8765/b.png": b"<html>not-image</html>"}

    async def fake_download(url: str) -> bytes:
        return responses[url]

    monkeypatch.setattr(link_media, "_download_image_bytes", fake_download)
    result = text_result("a: http://10.126.126.2:8765/a.png b: http://10.126.126.2:8765/b.png")

    registered = await link_media.enrich_tool_result_with_link_images(result)

    assert registered == 0
    assert [item.content_type for item in result.content_items] == ["text"]


@pytest.mark.asyncio
async def test_duplicate_urls_are_registered_once_and_capped_at_limit(monkeypatch):
    payload = png_bytes()

    async def fake_download(url: str) -> bytes:
        return payload

    monkeypatch.setattr(link_media, "_download_image_bytes", fake_download)
    urls = [f"http://10.126.126.2:8765/img/{index}.png" for index in range(5)]
    content = " ".join(urls + urls[:2])
    result = text_result(content)

    registered = await link_media.enrich_tool_result_with_link_images(result)

    assert registered == link_media.MAX_LINK_IMAGES
    source_urls = {item.metadata["source_url"] for item in result.content_items[1:]}
    assert len(source_urls) == link_media.MAX_LINK_IMAGES
    assert source_urls <= set(urls)


@pytest.mark.asyncio
async def test_failed_result_is_untouched(monkeypatch):
    async def fail_download(url: str) -> bytes:
        raise AssertionError("失败结果不应触发下载")

    monkeypatch.setattr(link_media, "_download_image_bytes", fail_download)
    result = text_result("图片链接：http://10.126.126.2:8765/hwimg/lan/d523.png", success=False)

    registered = await link_media.enrich_tool_result_with_link_images(result)

    assert registered == 0
    assert len(result.content_items) == 1


@pytest.mark.asyncio
async def test_resource_link_with_image_mime_and_without_extension_is_registered(monkeypatch):
    payload = png_bytes()

    async def fake_download(url: str) -> bytes:
        return payload

    monkeypatch.setattr(link_media, "_download_image_bytes", fake_download)
    result = ToolExecutionResult(tool_name="render", success=True, content="渲染完成")
    result.content_items.append(ToolContentItem(
        content_type="resource_link", uri="http://10.126.126.2:8765/render/12345", mime_type="image/png",
    ))

    registered = await link_media.enrich_tool_result_with_link_images(result)

    assert registered == 1
    item = result.content_items[-1]
    assert item.content_type == "image"
    assert item.metadata["source_url"] == "http://10.126.126.2:8765/render/12345"


@pytest.mark.asyncio
async def test_unreachable_download_returns_empty():
    # 真实下载路径：连接被拒时安静返回空字节，不抛异常
    downloaded = await link_media._download_image_bytes("http://127.0.0.1:1/x.png")
    assert downloaded == b""


def test_fetchable_url_rejects_non_http_and_credentials():
    assert link_media._is_fetchable_url("ftp://10.126.126.2/a.png") is False
    assert link_media._is_fetchable_url("http://user:pass@10.126.126.2/a.png") is False
    assert link_media._is_fetchable_url("http://10.126.126.2:8765/a.png") is True


@pytest.mark.asyncio
async def test_provider_invokes_enrichment_only_on_success(monkeypatch):
    from src.mcp_module import provider as provider_module

    calls: list[ToolExecutionResult] = []

    async def fake_enrich(result: ToolExecutionResult) -> int:
        calls.append(result)
        return 0

    monkeypatch.setattr(provider_module, "enrich_tool_result_with_link_images", fake_enrich)
    provider = provider_module.MCPToolProvider(service=Mock())
    invocation = Mock()

    async def invoke_success(invocation_arg, context):
        return ToolExecutionResult(tool_name="t", success=True, content="ok http://10.126.126.2/a.png")

    async def invoke_failure(invocation_arg, context):
        return ToolExecutionResult(tool_name="t", success=False, error_message="boom")

    provider._service.call_tool_invocation = invoke_success
    await provider.invoke(invocation, None)
    assert len(calls) == 1

    provider._service.call_tool_invocation = invoke_failure
    await provider.invoke(invocation, None)
    assert len(calls) == 1
