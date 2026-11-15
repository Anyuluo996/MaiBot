"""统一 WebUI 的认证协议，与现有登录/设置页面兼容。"""

from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url, parse_client_data_json
from webauthn.helpers.exceptions import (
    InvalidAuthenticationResponse,
    InvalidRegistrationResponse,
)
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field

import asyncio
import time

from .authentication import HostCredentials, LoginAttempts, MAX_PASSKEYS
from .passkey import (
    RP_NAME,
    WebAuthnChallenges,
    client_address,
    expected_origin,
    issue_authentication_options,
    issue_registration_options,
    rp_id_for_request,
    verify_assertion,
    verify_registration,
)

if TYPE_CHECKING:
    from .sessions import HostSessions


class VerifyRequest(BaseModel):
    token: str = Field(max_length=4096)


class UpdateRequest(BaseModel):
    new_token: str = Field(min_length=10, max_length=4096)


class ResetRequest(BaseModel):
    reset_password: bool = False


class PasskeyAssertionResponse(BaseModel):
    authenticatorData: str = Field(max_length=65536)
    clientDataJSON: str = Field(max_length=65536)
    signature: str = Field(max_length=65536)
    userHandle: Optional[str] = Field(default=None, max_length=256)


class PasskeyAssertionRequest(BaseModel):
    id: str = Field(min_length=1, max_length=2048)
    rawId: str = Field(min_length=1, max_length=2048)
    type: str = Field(max_length=64)
    response: PasskeyAssertionResponse


class PasskeyAttestationResponse(BaseModel):
    # 真实认证器的 attestationObject 携带证书链时可达数十 KB，上限放宽到 256KB。
    attestationObject: str = Field(max_length=262144)
    clientDataJSON: str = Field(max_length=65536)
    transports: Optional[List[str]] = None


class PasskeyRegistrationRequest(BaseModel):
    id: str = Field(min_length=1, max_length=2048)
    rawId: str = Field(min_length=1, max_length=2048)
    type: str = Field(max_length=64)
    response: PasskeyAttestationResponse
    name: Optional[str] = Field(default=None, max_length=100)


class PasskeyNameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)


def authenticate(request: Request, sessions: "HostSessions") -> bool:
    return sessions.valid(request.cookies.get(sessions.cookie_name))


def _challenge_from_credential(credential: Dict[str, Any]) -> Optional[bytes]:
    """从凭据的 clientDataJSON 里取出服务器签发的挑战字节；格式非法时返回 None。"""
    response = credential.get("response")
    encoded = response.get("clientDataJSON") if isinstance(response, dict) else None
    if not isinstance(encoded, str):
        return None
    try:
        return parse_client_data_json(base64url_to_bytes(encoded)).challenge
    except Exception:
        return None


def register_host_auth(
    app: FastAPI, sessions: "HostSessions", credentials: HostCredentials, require_session: Callable,
) -> None:
    attempts = LoginAttempts()
    challenges = WebAuthnChallenges()

    def record_attempt(address: str, success: bool) -> None:
        """失败计数打满时统一转 429。"""
        try:
            attempts.record(address, success)
        except ValueError as exc:
            raise HTTPException(429, str(exc)) from exc

    @app.get("/api/webui/auth/check")
    async def check(request: Request) -> Dict[str, Any]:
        authenticated = authenticate(request, sessions)
        if authenticated:
            return {"authenticated": True, **credentials.setup_status()}
        # 登录页据此决定是否展示通行密钥按钮；只暴露有无，不暴露任何凭据细节。
        return {"authenticated": False, "passkey_available": bool(credentials.passkeys())}

    @app.post("/api/webui/auth/verify")
    async def verify(body: VerifyRequest, request: Request, response: Response) -> Dict[str, Any]:
        address = request.client.host if request.client else "unknown"
        if attempts.blocked(address):
            raise HTTPException(429, "认证失败次数过多，请稍后再试")
        valid = credentials.verify(body.token)
        record_attempt(address, valid)
        if not valid:
            return {"valid": False, "message": "Token 无效或已过期"}
        sessions.set_cookie(response, await asyncio.to_thread(sessions.create_session, request.headers.get("origin")))
        return {"valid": True, "message": "登录成功", **credentials.setup_status()}

    @app.post("/api/webui/auth/logout")
    async def logout(request: Request, response: Response) -> Dict[str, Any]:
        await asyncio.to_thread(sessions.revoke, request.cookies.get(sessions.cookie_name))
        response.delete_cookie(sessions.cookie_name, path="/")
        return {"success": True, "message": "已登出"}

    @app.post("/api/webui/auth/update", dependencies=[Depends(require_session)])
    async def update(body: UpdateRequest, response: Response) -> Dict[str, Any]:
        try:
            await asyncio.to_thread(credentials.update, body.new_token)
        except ValueError as exc:
            return {"success": False, "message": str(exc)}
        await asyncio.to_thread(sessions.clear_all)
        response.delete_cookie(sessions.cookie_name, path="/")
        return {"success": True, "message": "密码已更新，请重新登录"}

    @app.post("/api/webui/auth/regenerate", dependencies=[Depends(require_session)])
    async def regenerate(response: Response) -> Dict[str, Any]:
        token = await asyncio.to_thread(credentials.regenerate)
        await asyncio.to_thread(sessions.clear_all)
        response.delete_cookie(sessions.cookie_name, path="/")
        return {"success": True, "token": token, "message": "密码已重新生成"}

    @app.get("/api/webui/setup/status", dependencies=[Depends(require_session)])
    async def setup_status() -> Dict[str, Any]:
        return {**credentials.setup_status(), "message": "统一 WebUI 配置状态"}

    @app.post("/api/webui/setup/complete", dependencies=[Depends(require_session)])
    async def complete() -> Dict[str, Any]:
        try:
            await asyncio.to_thread(credentials.complete_setup)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"success": True, "message": "配置完成"}

    @app.post("/api/webui/setup/reset", dependencies=[Depends(require_session)])
    async def reset(body: ResetRequest, request: Request, response: Response) -> Dict[str, Any]:
        await asyncio.to_thread(credentials.reset_setup, body.reset_password)
        if body.reset_password:
            await asyncio.to_thread(sessions.clear_all)
            sessions.set_cookie(response, await asyncio.to_thread(sessions.create_session, request.headers.get("origin")))
        return {"success": True, "message": "配置状态已重置"}

    @app.get("/api/webui/auth/passkey/options")
    async def passkey_options(request: Request) -> Response:
        # 匿名端点不下发已注册凭据 ID（防止枚举）；注册走 resident_key=PREFERRED，
        # 平台认证器（Touch ID / Windows Hello / 手机通行密钥）均为可发现凭据，浏览器能自行列出。
        options = issue_authentication_options(request, challenges, [])
        return Response(content=options, media_type="application/json")

    @app.post("/api/webui/auth/passkey/login")
    async def passkey_login(body: PasskeyAssertionRequest, request: Request, response: Response) -> Dict[str, Any]:
        address = client_address(request)
        if attempts.blocked(address):
            raise HTTPException(429, "认证失败次数过多，请稍后再试")
        credential = body.model_dump()
        try:
            challenge = _challenge_from_credential(credential)
            if challenge is None or not challenges.take(bytes_to_base64url(challenge)):
                raise InvalidAuthenticationResponse("挑战已失效")
            record = credentials.passkey(body.id)
            if record is None:
                raise InvalidAuthenticationResponse("未知通行密钥")
            claimed_handle = credential["response"].get("userHandle")
            if claimed_handle is not None and claimed_handle != await asyncio.to_thread(credentials.webauthn_user_handle):
                raise InvalidAuthenticationResponse("用户句柄不匹配")
            new_count, backed_up = verify_assertion(
                credential,
                challenge=challenge,
                rp_id=rp_id_for_request(request),
                origin=expected_origin(request),
                credential_public_key=base64url_to_bytes(record["public_key"]),
                sign_count=int(record.get("sign_count") or 0),
            )
        except (InvalidAuthenticationResponse, ValueError):
            record_attempt(address, False)
            return {"valid": False, "message": "通行密钥验证失败"}
        except HTTPException:
            # Origin 缺失或畸形等 4xx 也计入失败，避免绕过限流计数。
            record_attempt(address, False)
            raise
        record_attempt(address, True)
        still_registered = await asyncio.to_thread(
            credentials.touch_passkey, body.id, sign_count=new_count, backed_up=backed_up,
        )
        if not still_registered:
            # 验证期间凭据被并发删除（如失陷恢复重置），不得签发会话。
            return {"valid": False, "message": "该通行密钥已被移除"}
        sessions.set_cookie(response, await asyncio.to_thread(sessions.create_session, request.headers.get("origin")))
        return {"valid": True, "message": "登录成功", **credentials.setup_status()}

    @app.get("/api/webui/auth/passkeys", dependencies=[Depends(require_session)])
    async def passkey_list() -> Dict[str, Any]:
        return {"passkeys": [_passkey_view(record) for record in credentials.passkeys()]}

    @app.post("/api/webui/auth/passkey/register/options", dependencies=[Depends(require_session)])
    async def passkey_register_options(request: Request) -> Response:
        user_handle = base64url_to_bytes(await asyncio.to_thread(credentials.webauthn_user_handle))
        options = issue_registration_options(request, challenges, user_handle, credentials.passkeys())
        return Response(content=options, media_type="application/json")

    @app.post("/api/webui/auth/passkey/register", dependencies=[Depends(require_session)])
    async def passkey_register(
        body: PasskeyRegistrationRequest, request: Request, response: Response,
    ) -> Dict[str, Any]:
        if len(credentials.passkeys()) >= MAX_PASSKEYS:
            raise HTTPException(409, "通行密钥数量已达上限")
        credential = body.model_dump()
        challenge = _challenge_from_credential(credential)
        if challenge is None or not challenges.take(bytes_to_base64url(challenge)):
            return {"success": False, "message": "注册会话已过期，请重新开始"}
        if credentials.passkey(body.id) is not None:
            return {"success": False, "message": "该通行密钥已经注册过"}
        try:
            credential_id, public_key, sign_count, transports = verify_registration(
                credential,
                challenge=challenge,
                rp_id=rp_id_for_request(request),
                origin=expected_origin(request),
            )
        except (InvalidRegistrationResponse, ValueError):
            return {"success": False, "message": "通行密钥注册失败，认证器响应无效"}
        if credential_id != body.id:
            # attested credential ID 必须与客户端声明的 id 一致，否则去重检查可被绕过。
            return {"success": False, "message": "通行密钥注册失败，认证器响应无效"}
        name = body.name.strip() if isinstance(body.name, str) and body.name.strip() else RP_NAME
        await asyncio.to_thread(credentials.put_passkey, {
            "credential_id": credential_id,
            "public_key": bytes_to_base64url(public_key),
            "sign_count": sign_count,
            "transports": transports,
            "name": name,
            "created_at": time.time(),
            "last_used_at": None,
            "backed_up": False,
        })
        return {"success": True, "message": "通行密钥已添加", "credential_id": credential_id}

    @app.patch("/api/webui/auth/passkeys/{credential_id}", dependencies=[Depends(require_session)])
    async def passkey_rename(credential_id: str, body: PasskeyNameRequest) -> Dict[str, Any]:
        renamed = await asyncio.to_thread(
            credentials.update_passkey_name,
            credential_id,
            body.name.strip() or RP_NAME,
        )
        if not renamed:
            raise HTTPException(404, "未找到该通行密钥")
        return {"success": True, "message": "通行密钥名称已更新"}

    @app.delete("/api/webui/auth/passkeys/{credential_id}", dependencies=[Depends(require_session)])
    async def passkey_delete(credential_id: str) -> Dict[str, Any]:
        if not await asyncio.to_thread(credentials.drop_passkey, credential_id):
            raise HTTPException(404, "未找到该通行密钥")
        return {"success": True, "message": "通行密钥已删除"}


def _passkey_view(record: Dict[str, Any]) -> Dict[str, Any]:
    """对外只暴露管理与展示字段，公钥与签名计数不离开宿主。"""
    return {
        "credential_id": record.get("credential_id"),
        "name": record.get("name") or RP_NAME,
        "created_at": record.get("created_at"),
        "last_used_at": record.get("last_used_at"),
        "transports": record.get("transports"),
        "backed_up": bool(record.get("backed_up")),
    }
