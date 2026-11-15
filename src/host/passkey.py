"""WebAuthn 通行密钥：一次性挑战、RP 信息推导与注册/断言校验；凭据持久化由 HostCredentials 承担。

RP ID 与期望来源都从浏览器侧的 Origin / Host 头推导（ ceremony 与 fetch 同源，客户端已强制
rpId 为请求域名后缀），无需额外配置字段；反代剥端口等场景与 same_webui_origin 的判定一致。
"""

from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
)

import time


RP_NAME = "MaiBot WebUI"
CHALLENGE_TTL_SECONDS = 300.0
_MAX_CHALLENGES = 1024
_MAX_CHALLENGES_PER_OWNER = 8
AUTHENTICATOR_SELECTION = AuthenticatorSelectionCriteria(resident_key=ResidentKeyRequirement.PREFERRED)


class WebAuthnChallenges:
    """仪式挑战只存内存、单次使用、限时失效；键为挑战的 base64url 文本。

    全局上限防内存膨胀；按来源地址限额防止匿名 options 端点被单点灌满。
    """

    def __init__(
        self,
        ttl_seconds: float = CHALLENGE_TTL_SECONDS,
        max_entries: int = _MAX_CHALLENGES,
        max_entries_per_owner: int = _MAX_CHALLENGES_PER_OWNER,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._max_entries_per_owner = max_entries_per_owner
        self._entries: Dict[str, Tuple[float, str]] = {}

    def store(self, challenge: str, owner: str) -> None:
        now = time.monotonic()
        self._entries = {key: entry for key, entry in self._entries.items() if entry[0] > now}
        if len(self._entries) >= self._max_entries:
            raise HTTPException(429, "通行密钥请求过多，请稍后再试")
        same_owner = sum(1 for entry in self._entries.values() if entry[1] == owner)
        if same_owner >= self._max_entries_per_owner:
            raise HTTPException(429, "通行密钥请求过于频繁，请稍后再试")
        self._entries[challenge] = (now + self._ttl_seconds, owner)

    def take(self, challenge: str) -> bool:
        entry = self._entries.pop(challenge, None)
        return entry is not None and entry[0] > time.monotonic()


def _origin_hostname(origin: str) -> Optional[str]:
    try:
        return urlsplit(origin).hostname
    except ValueError:
        # urlsplit 对畸形 IPv6 字面量等输入会抛错，视为无主机名。
        return None


def rp_id_for_request(request: Request) -> str:
    """RP ID 取 Origin 的主机名；GET 请求通常没有 Origin，回退到 Host 头的主机名。"""
    origin = request.headers.get("origin")
    if origin:
        hostname = _origin_hostname(origin)
        if hostname:
            return hostname.lower()
    host = request.headers.get("host", "")
    hostname = _origin_hostname(f"http://{host}") if host else None
    if not hostname:
        raise HTTPException(400, "无法确定 WebUI 地址，无法使用通行密钥")
    return hostname.lower()


def expected_origin(request: Request) -> str:
    """断言/注册验证的期望来源就是发起请求的浏览器 Origin；缺失即拒绝，不猜测。"""
    origin = request.headers.get("origin")
    if not origin:
        raise HTTPException(400, "请求缺少 Origin，无法完成通行密钥验证")
    parsed_hostname = _origin_hostname(origin)
    if parsed_hostname is None:
        raise HTTPException(400, "请求 Origin 无效，无法完成通行密钥验证")
    scheme = origin.split("://", 1)[0]
    if scheme not in {"http", "https"}:
        raise HTTPException(400, "请求 Origin 无效，无法完成通行密钥验证")
    return origin


def allow_credentials(records: List[Dict[str, Any]]) -> Optional[List[PublicKeyCredentialDescriptor]]:
    """已注册凭据作为允许列表；为空时返回 None，浏览器改为展示该 RP 的全部可发现凭据。"""
    if not records:
        return None
    return [PublicKeyCredentialDescriptor(id=base64url_to_bytes(record["credential_id"])) for record in records]


def issue_authentication_options(request: Request, challenges: WebAuthnChallenges, records: List[Dict[str, Any]]) -> str:
    options = generate_authentication_options(
        rp_id=rp_id_for_request(request),
        allow_credentials=allow_credentials(records),
    )
    challenges.store(bytes_to_base64url(options.challenge), client_address(request))
    return options_to_json(options)


def issue_registration_options(
    request: Request, challenges: WebAuthnChallenges, user_handle: bytes, records: List[Dict[str, Any]],
) -> str:
    options = generate_registration_options(
        rp_id=rp_id_for_request(request),
        rp_name=RP_NAME,
        user_name="maibot",
        user_id=user_handle,
        exclude_credentials=allow_credentials(records),
        authenticator_selection=AUTHENTICATOR_SELECTION,
    )
    challenges.store(bytes_to_base64url(options.challenge), client_address(request))
    return options_to_json(options)


def client_address(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def verify_registration(
    credential: Dict[str, Any], *, challenge: bytes, rp_id: str, origin: str,
) -> Tuple[str, bytes, int, Optional[List[str]]]:
    """校验注册响应，返回 (credential_id, 公钥 COSE 字节, 初始签名计数, transports)。"""
    verification = verify_registration_response(
        credential=credential,
        expected_challenge=challenge,
        expected_rp_id=rp_id,
        expected_origin=origin,
    )
    transports = None
    response = credential.get("response")
    if isinstance(response, dict) and isinstance(response.get("transports"), list):
        transports = [name for name in response["transports"] if isinstance(name, str)]
    return (
        bytes_to_base64url(verification.credential_id),
        verification.credential_public_key,
        verification.sign_count,
        transports,
    )


def verify_assertion(
    credential: Dict[str, Any], *, challenge: bytes, rp_id: str, origin: str,
    credential_public_key: bytes, sign_count: int,
) -> Tuple[int, bool]:
    """校验登录断言，返回 (新签名计数, 是否已备份)。"""
    verification = verify_authentication_response(
        credential=credential,
        expected_challenge=challenge,
        expected_rp_id=rp_id,
        expected_origin=origin,
        credential_public_key=credential_public_key,
        credential_current_sign_count=sign_count,
    )
    return verification.new_sign_count, verification.credential_backed_up


__all__ = [
    "RP_NAME",
    "WebAuthnChallenges",
    "expected_origin",
    "issue_authentication_options",
    "issue_registration_options",
    "rp_id_for_request",
    "verify_assertion",
    "verify_registration",
]
