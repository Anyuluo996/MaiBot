"""将 MCP 工具结果中的图片链接注册为媒体内容项。

gscore 等图床型 MCP 工具只返回文本 + 图片 URL，不会注册任何媒体，
模型用 attach_pic 引用时拿不到索引。这里在 MCP Provider 边界把结果
中的图片链接下载并注入为 image 内容项，媒体拆分、`<tool_result_media_list>`
索引注解与 reply 引用链路随之自动生效。

MCP server 是本机配置的可信进程，其返回的内网图床 URL 是主用例，
因此这里只校验 scheme 与凭证，不做公网地址限制。
"""

from base64 import b64encode
from io import BytesIO
from typing import List
from urllib.parse import urlsplit

import asyncio
import re

import httpx
from PIL import Image

from src.common.http_client import get_shared_ssl_context_async
from src.common.logger import get_logger
from src.core.tooling import ToolContentItem, ToolExecutionResult

logger = get_logger("mcp_link_media")

# 单次工具结果最多注册的链接图片数与单图大小上限
MAX_LINK_IMAGES = 3
MAX_IMAGE_BYTES = 5 * 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 10.0

# 从文本中提取图片直链；后缀后禁止紧跟字母，避免 x.gifify 之类误截断
_IMAGE_URL_PATTERN = re.compile(
    r"https?://[^\s<>\"'（）【】]+?\.(?:png|jpe?g|gif|webp|bmp)(?:\?[^\s<>\"'（）【】]*)?(?![A-Za-z])",
    re.IGNORECASE,
)
_IMAGE_MIME_PREFIX = "image/"


def _is_fetchable_url(url: str) -> bool:
    """链接媒体只接受不含凭证的 HTTP(S) 直链。"""

    parsed = urlsplit(url)
    return parsed.scheme in ("http", "https") and bool(parsed.hostname) and not parsed.username and not parsed.password


def _sniff_image_mime(image_bytes: bytes) -> str:
    """以 PIL 魔数嗅探真实格式，非图片数据返回空串。"""

    try:
        with Image.open(BytesIO(image_bytes)) as image:
            image_format = (image.format or "").strip().lower()
        return f"{_IMAGE_MIME_PREFIX}{image_format}" if image_format else ""
    except Exception:
        return ""


def _extract_image_urls(result: ToolExecutionResult) -> List[str]:
    """按出现顺序提取结果中的图片链接，逐项去重并截断到上限。"""

    urls: List[str] = []

    def add_url(url: str) -> None:
        if _is_fetchable_url(url) and url not in urls:
            urls.append(url)

    for item in result.content_items:
        if item.content_type == "text":
            for match in _IMAGE_URL_PATTERN.finditer(item.text):
                add_url(match.group(0))
        elif item.content_type in ("resource_link", "resource"):
            uri = item.uri.strip()
            if item.mime_type.strip().lower().startswith(_IMAGE_MIME_PREFIX):
                # mime 声明为图片但无后缀的资源链接直接收下，交给下载后的嗅探把关
                add_url(uri)
            else:
                for match in _IMAGE_URL_PATTERN.finditer(uri):
                    add_url(match.group(0))
        if len(urls) >= MAX_LINK_IMAGES:
            break
    return urls[:MAX_LINK_IMAGES]


async def _download_image_bytes(url: str) -> bytes:
    """下载链接内容，失败或超过大小上限时返回空字节。"""

    try:
        ssl_context = await get_shared_ssl_context_async()
        async with httpx.AsyncClient(
            verify=ssl_context, follow_redirects=True, trust_env=False,
            timeout=httpx.Timeout(DOWNLOAD_TIMEOUT_SECONDS),
        ) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                chunks: List[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_IMAGE_BYTES:
                        logger.debug(f"[链接注册媒体] 图片超过大小上限，跳过: url={url} size>={total}")
                        return b""
                    chunks.append(chunk)
                return b"".join(chunks)
    except (httpx.HTTPError, asyncio.TimeoutError) as exc:
        logger.debug(f"[链接注册媒体] 图片下载失败，跳过: url={url} error={exc}")
        return b""


async def enrich_tool_result_with_link_images(result: ToolExecutionResult) -> int:
    """把工具结果中的图片链接下载注册为 image 内容项，返回注册数量。

    任何失败都只影响新增的媒体项，原有结果内容保持不变。
    """

    if not result.success:
        return 0
    urls = _extract_image_urls(result)
    if not urls:
        return 0

    downloaded = await asyncio.gather(*(_download_image_bytes(url) for url in urls))
    registered = 0
    for url, image_bytes in zip(urls, downloaded):
        if not image_bytes:
            continue
        mime_type = _sniff_image_mime(image_bytes)
        if not mime_type:
            logger.debug(f"[链接注册媒体] 响应不是图片数据，跳过: url={url}")
            continue
        result.content_items.append(
            ToolContentItem(
                content_type="image",
                data=b64encode(image_bytes).decode("ascii"),
                mime_type=mime_type,
                metadata={"source_url": url},
            )
        )
        registered += 1
        logger.debug(f"[链接注册媒体] 已注册工具返回的图片链接: url={url} mime={mime_type} size={len(image_bytes)}")
    return registered
