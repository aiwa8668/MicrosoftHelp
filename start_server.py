from os import environ
import base64
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import time
from typing import Any
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request as UrlRequest, urlopen
from uuid import uuid4

from aiohttp import ClientSession, ClientTimeout
from aiohttp.web import Application, FileResponse, HTTPFound, HTTPNotFound, Request, Response, json_response, run_app
from jinja2 import Environment, FileSystemLoader
from microsoft_agents.hosting.aiohttp import (
    CloudAdapter,
    jwt_authorization_middleware,
    start_agent_process,
)
from microsoft_agents.hosting.core import AgentApplication, AgentAuthConfiguration

from faq import (
    admin_accounts_change_password,
    admin_accounts_create,
    admin_accounts_delete,
    admin_accounts_list,
    admin_mfa_disable,
    admin_mfa_enable,
    admin_mfa_setup,
    admin_accounts_update,
    admin_delete,
    admin_dashboard,
    admin_edit,
    admin_faq_image_upload,
    admin_faq_import_content,
    admin_list,
    admin_login,
    admin_login_captcha,
    admin_login_method,
    admin_logs,
    admin_logout,
    admin_new,
    admin_webchat_access_list,
    admin_webchat_access_update,
    ensure_webchat_access,
    faq_directory,
    faq_detail,
    faq_image,
    initialize_database,
    record_webchat_question,
)


TEMPLATES_DIR = Path(__file__).parent / "templates"
_TEMPLATE_ENV = Environment(loader=FileSystemLoader(TEMPLATES_DIR), autoescape=True)
_WEBCHAT_LOGIN_HTML = (TEMPLATES_DIR / "webchat_login.html").read_text(encoding="utf-8")
_WEBCHAT_HTML = (TEMPLATES_DIR / "webchat.html").read_text(encoding="utf-8")
WEBCHAT_UPLOAD_DIR = Path(__file__).parent / "data" / "uploads"
MAX_UPLOAD_SIZE = 5 * 1024 * 1024
DEFAULT_WEBCHAT_UPLOAD_RETENTION_DAYS = 10
_TOKEN_CACHE: dict[str, Any] = {"access_token": "", "expires_at": 0.0}
_LOCAL_CONVERSATIONS: dict[str, list[dict[str, Any]]] = {}
_DIRECTLINE_CONVERSATIONS: dict[str, dict[str, Any]] = {}

# ── Entra ID OAuth ──────────────────────────────────────────────────────────
try:
    import msal as _msal  # type: ignore
    _MSAL_AVAILABLE = True
except ImportError:
    _msal = None  # type: ignore
    _MSAL_AVAILABLE = False

_ENTRA_SESSION_COOKIE = "webchat_entra_session"
_ENTRA_SESSION_TTL = 8 * 3600
_ENTRA_FLOWS: dict[str, dict] = {}    # state_token -> {flow, expires_at}
_ENTRA_SESSIONS: dict[str, dict] = {} # session_token -> {user, expires_at}
_ENTRA_SECRET: bytes = secrets.token_bytes(32)


def _entra_enabled() -> bool:
    """仅当 msal 可用且 client_id / client_secret 均已配置时才启用 Entra 认证。"""
    return bool(
        _MSAL_AVAILABLE
        and environ.get("ENTRA_CLIENT_ID", "").strip()
        and environ.get("ENTRA_CLIENT_SECRET", "").strip()
    )


def _entra_china_enabled() -> bool:
    return bool(
        _MSAL_AVAILABLE
        and environ.get("ENTRA_CHINA_CLIENT_ID", "").strip()
        and environ.get("ENTRA_CHINA_CLIENT_SECRET", "").strip()
    )


async def _admin_connection_status(_: Request) -> Response:
    backend = _get_webchat_backend()
    if backend == "local":
        return json_response({"connected": True, "message": "本地服务正常"})
    if backend == "directline":
        connected = bool(_read_env("COPILOT_STUDIO_DIRECTLINE_TOKEN_ENDPOINT", "DIRECTLINE_TOKEN_ENDPOINT"))
        return json_response({"connected": connected, "message": "已连接" if connected else "缺少 Direct Line 配置"})
    connected = bool(_read_env("COPILOT_STUDIO_CONVERSATIONS_URL"))
    return json_response({"connected": connected, "message": "已连接" if connected else "缺少服务端配置"})


def _entra_build_msal_app() -> "_msal.ConfidentialClientApplication":
    """构建 MSAL ConfidentialClientApplication。

    authority 默认使用 organizations 端点，允许任意组织租户（含外部）用户登录。
    如需限定单租户，可在 .env 中将 ENTRA_AUTHORITY 设为具体 tenant_id URL。
    """
    client_id = environ.get("ENTRA_CLIENT_ID", "").strip()
    client_secret = environ.get("ENTRA_CLIENT_SECRET", "").strip()
    authority = (
        environ.get("ENTRA_AUTHORITY", "").strip()
        or "https://login.microsoftonline.com/organizations"
    )
    return _msal.ConfidentialClientApplication(
        client_id,
        client_credential=client_secret,
        authority=authority,
    )


def _entra_build_china_msal_app() -> "_msal.ConfidentialClientApplication":
    authority = (
        environ.get("ENTRA_CHINA_AUTHORITY", "").strip()
        or "https://login.partner.microsoftonline.cn/organizations"
    )
    return _msal.ConfidentialClientApplication(
        environ.get("ENTRA_CHINA_CLIENT_ID", "").strip(),
        client_credential=environ.get("ENTRA_CHINA_CLIENT_SECRET", "").strip(),
        authority=authority,
    )


def _entra_sign(payload: str) -> str:
    """用 HMAC-SHA256 签名 payload，密钥优先从环境变量读取。"""
    key_str = environ.get("ENTRA_SESSION_SECRET", "") or environ.get("FAQ_SESSION_SECRET", "")
    key: bytes = key_str.encode() if key_str else _ENTRA_SECRET
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


def _entra_get_session(request: Request) -> dict | None:
    """从 Cookie 恢复并验证 Entra 会话，过期或无效时返回 None。"""
    raw = request.cookies.get(_ENTRA_SESSION_COOKIE, "")
    if not raw or "." not in raw:
        return None
    token, sig = raw.rsplit(".", 1)
    if not secrets.compare_digest(_entra_sign(token), sig):
        return None
    data = _ENTRA_SESSIONS.get(token)
    if data and float(data.get("expires_at", 0)) > time.time():
        return data
    _ENTRA_SESSIONS.pop(token, None)
    return None


def _entra_make_session_cookie(user: dict, access_token: str = "") -> str:
    """创建服务端会话并返回签名 Cookie 值。"""
    token = secrets.token_urlsafe(32)
    _ENTRA_SESSIONS[token] = {
        "user": user,
        "access_token": access_token,
        "expires_at": time.time() + _ENTRA_SESSION_TTL
    }
    return f"{token}.{_entra_sign(token)}"


def _entra_get_redirect_uri(request: Request) -> str:
    """优先读取 .env 中配置的重定向 URI，否则自动构造。"""
    configured = environ.get("ENTRA_REDIRECT_URI", "").strip()
    if configured:
        return configured
    proto = request.headers.get("X-Forwarded-Proto", "http")
    host = request.headers.get("X-Forwarded-Host", request.host)
    return f"{proto}://{host}/entra/callback"


def _entra_get_china_redirect_uri(request: Request) -> str:
    configured = environ.get("ENTRA_CHINA_REDIRECT_URI", "").strip()
    if configured:
        return configured
    proto = request.headers.get("X-Forwarded-Proto", "http")
    host = request.headers.get("X-Forwarded-Host", request.host)
    return f"{proto}://{host}/entra/china/callback"


def _entra_scopes(environment_key: str, default_scope: str) -> list[str]:
    scopes_raw = environ.get(environment_key, default_scope).strip()
    reserved_scopes = {"openid", "profile", "offline_access"}
    scopes = [scope for scope in scopes_raw.split() if scope and scope not in reserved_scopes]
    return scopes or [default_scope]


def _entra_login_account(session: dict | None) -> str:
    user = (session or {}).get("user", {})
    if not isinstance(user, dict):
        return ""
    return str(user.get("username") or user.get("name") or "").strip()


def _approved_webchat_user(request: Request) -> tuple[dict | None, str, str]:
    if not (_entra_enabled() or _entra_china_enabled()):
        return {"anonymous": True}, "approved", ""
    session = _entra_get_session(request)
    if session is None:
        return None, "", ""
    user = session.get("user", {})
    if not isinstance(user, dict):
        return None, "", ""
    status, denial_reason = ensure_webchat_access(user)
    return (user if status == "approved" else {}), status, denial_reason


def _webchat_access_denied(request: Request) -> Response | None:
    user, status, _denial_reason = _approved_webchat_user(request)
    if user is None:
        return json_response({"error": "请先登录"}, status=401)
    if user == {} and (_entra_enabled() or _entra_china_enabled()):
        message = "访问申请已被拒绝" if status == "denied" else "登录成功，等待管理员审批"
        return json_response({"error": message}, status=403)
    return None


async def _entra_login_route(request: Request) -> Response:
    """GET /entra/login — 发起 Microsoft Entra ID 授权码登录。"""
    if not _entra_enabled():
        raise HTTPFound("/webchat")
    msal_app = _entra_build_msal_app()
    redirect_uri = _entra_get_redirect_uri(request)
    flow = msal_app.initiate_auth_code_flow(
        scopes=_entra_scopes("ENTRA_SCOPES", "https://graph.microsoft.com/User.Read"),
        redirect_uri=redirect_uri,
        prompt="select_account",
        response_mode="form_post",
    )
    # 使用 MSAL 实际写入 auth_uri 的 state 作为键，确保回调时精确匹配
    flow_state = str(flow.get("state", ""))
    _ENTRA_FLOWS[flow_state] = {"flow": flow, "provider": "global", "expires_at": time.time() + 900}
    raise HTTPFound(flow["auth_uri"])


async def _entra_china_login_route(request: Request) -> Response:
    if not _entra_china_enabled():
        raise HTTPFound("/webchat?entra_error=" + quote("世纪互联 Entra ID 登录尚未配置"))
    # #region debug-point A:china-authority
    _debug_url = "http://127.0.0.1:7777/event"
    _debug_session = "china-entra-login"
    try:
        _debug_values = (Path(__file__).parent / ".dbg" / "china-entra-login.env").read_text(encoding="utf-8").splitlines()
        _debug_url = next((value.split("=", 1)[1] for value in _debug_values if value.startswith("DEBUG_SERVER_URL=")), _debug_url)
        _debug_session = next((value.split("=", 1)[1] for value in _debug_values if value.startswith("DEBUG_SESSION_ID=")), _debug_session)
    except OSError:
        pass
    _debug_authority = environ.get("ENTRA_CHINA_AUTHORITY", "").strip()
    try:
        urlopen(UrlRequest(_debug_url, data=json.dumps({"sessionId": _debug_session, "runId": "pre-fix", "hypothesisId": "A", "location": "start_server.py:_entra_china_login_route", "msg": "[DEBUG] 开始世纪互联登录", "data": {"authority_configured": bool(_debug_authority), "authority_has_placeholder": "<" in _debug_authority or ">" in _debug_authority, "authority_host": urlparse(_debug_authority).netloc}}).encode(), headers={"Content-Type": "application/json"}), timeout=1).read()
    except Exception:
        pass
    # #endregion
    flow = _entra_build_china_msal_app().initiate_auth_code_flow(
        scopes=_entra_scopes("ENTRA_CHINA_SCOPES", "https://microsoftgraph.chinacloudapi.cn/User.Read"),
        redirect_uri=_entra_get_china_redirect_uri(request),
        prompt="select_account",
        response_mode="form_post",
    )
    flow_state = str(flow.get("state", ""))
    _ENTRA_FLOWS[flow_state] = {"flow": flow, "provider": "china", "expires_at": time.time() + 900}
    raise HTTPFound(flow["auth_uri"])


async def _entra_callback_route(request: Request) -> Response:
    """GET|POST /entra/callback — 处理 Microsoft Entra ID OAuth 授权码回调。
    
    支持两种模式：
    - GET：response_mode=query（默认，参数在 URL 中）
    - POST：response_mode=form_post（推荐，参数在请求体中）
    """
    # 优先读取 POST 表单数据（form_post 模式），再尝试 URL 查询参数（query 模式）
    if request.method == "POST":
        params = dict(await request.post())
    else:
        params = dict(request.rel_url.query)
    state = params.get("state", "")
    flow_data = _ENTRA_FLOWS.pop(state, None)
    if not flow_data or flow_data.get("provider") != "global" or flow_data.get("expires_at", 0) < time.time():
        raise HTTPFound("/webchat?entra_error=" + quote("登录会话已过期，请重新登录"))
    if not _entra_enabled():
        raise HTTPFound("/webchat")
    msal_app = _entra_build_msal_app()
    try:
        result = msal_app.acquire_token_by_auth_code_flow(flow_data["flow"], params)
    except Exception as exc:  # noqa: BLE001
        raise HTTPFound("/webchat?entra_error=" + quote(f"Entra 登录失败：{str(exc)[:80]}"))
    if not isinstance(result, dict) or result.get("error"):
        err = (result or {}).get("error_description") or (result or {}).get("error") or "未知错误"
        raise HTTPFound("/webchat?entra_error=" + quote(str(err)[:120]))
    claims = result.get("id_token_claims") or {}
    username = str(
        claims.get("preferred_username") or claims.get("upn") or claims.get("email") or claims.get("unique_name") or claims.get("oid") or ""
    ).strip()
    display_name = str(claims.get("name") or username).strip()
    tenant_id = str(claims.get("tid") or "").strip()
    access_token = result.get("access_token", "")
    cookie_val = _entra_make_session_cookie({"name": display_name, "username": username, "tenantId": tenant_id, "objectId": str(claims.get("oid") or "").strip()}, access_token)
    resp = Response(status=302)
    resp.headers["Location"] = "/webchat"
    resp.set_cookie(
        _ENTRA_SESSION_COOKIE,
        cookie_val,
        max_age=_ENTRA_SESSION_TTL,
        httponly=True,
        samesite="Lax",
        secure=request.secure,
        path="/",
    )
    return resp


async def _entra_china_callback_route(request: Request) -> Response:
    if request.method == "POST":
        params = dict(await request.post())
    else:
        params = dict(request.rel_url.query)
    state = params.get("state", "")
    flow_data = _ENTRA_FLOWS.pop(state, None)
    if not flow_data or flow_data.get("provider") != "china" or flow_data.get("expires_at", 0) < time.time():
        raise HTTPFound("/webchat?entra_error=" + quote("登录会话已过期，请重新登录"))
    if not _entra_china_enabled():
        raise HTTPFound("/webchat")
    try:
        result = _entra_build_china_msal_app().acquire_token_by_auth_code_flow(flow_data["flow"], params)
    except Exception as exc:  # noqa: BLE001
        raise HTTPFound("/webchat?entra_error=" + quote(f"世纪互联 Entra 登录失败：{str(exc)[:80]}"))
    if not isinstance(result, dict) or result.get("error"):
        err = (result or {}).get("error_description") or (result or {}).get("error") or "未知错误"
        raise HTTPFound("/webchat?entra_error=" + quote(str(err)[:120]))
    claims = result.get("id_token_claims") or {}
    username = str(claims.get("preferred_username") or claims.get("upn") or claims.get("email") or claims.get("unique_name") or claims.get("oid") or "").strip()
    display_name = str(claims.get("name") or username).strip()
    tenant_id = str(claims.get("tid") or "").strip()
    user = {
        "name": display_name,
        "username": username,
        "tenantId": tenant_id,
        "objectId": str(claims.get("oid") or "").strip(),
        "graph_base_url": "https://microsoftgraph.chinacloudapi.cn",
    }
    cookie_val = _entra_make_session_cookie(user, result.get("access_token", ""))
    resp = Response(status=302)
    resp.headers["Location"] = "/webchat"
    resp.set_cookie(
        _ENTRA_SESSION_COOKIE,
        cookie_val,
        max_age=_ENTRA_SESSION_TTL,
        httponly=True,
        samesite="Lax",
        secure=request.secure,
        path="/",
    )
    return resp


async def _entra_logout_route(request: Request) -> Response:
    """GET|POST /entra/logout — 清除 Entra 会话并重定向至主页。"""
    raw = request.cookies.get(_ENTRA_SESSION_COOKIE, "")
    if raw and "." in raw:
        token = raw.rsplit(".", 1)[0]
        _ENTRA_SESSIONS.pop(token, None)
    resp = Response(status=302)
    resp.headers["Location"] = "/webchat"
    resp.del_cookie(_ENTRA_SESSION_COOKIE, path="/")
    return resp


async def _entra_auth_status_route(request: Request) -> Response:
    """GET /entra/auth-status — 返回当前 Entra 认证状态（JSON）。"""
    sess = _entra_get_session(request)
    enabled = _entra_enabled()
    if sess:
        user = sess.get("user", {})
        return json_response({
            "enabled": enabled,
            "authenticated": True,
            "user": {"name": user.get("name", ""), "username": user.get("username", ""), "tenantId": user.get("tenantId", "")},
        })
    return json_response({"enabled": enabled, "authenticated": False, "user": {"name": "", "username": "", "tenantId": ""}})


async def _entra_photo_route(request: Request) -> Response:
    """GET /entra/photo — 返回当前用户的 Microsoft Graph 头像（如果可用）。"""
    sess = _entra_get_session(request)
    if not sess:
        raise HTTPNotFound()
    
    access_token = sess.get("access_token", "")
    if not access_token:
        raise HTTPNotFound()
    
    try:
        # 调用 Microsoft Graph API 获取用户照片
        import aiohttp  # noqa: F401, E402
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{sess.get('user', {}).get('graph_base_url', 'https://graph.microsoft.com')}/v1.0/me/photo/$value",
                headers={"Authorization": f"Bearer {access_token}"}
            ) as resp:
                if resp.status == 200:
                    photo_data = await resp.read()
                    return Response(
                        body=photo_data,
                        status=200,
                        content_type="image/jpeg"
                    )
        raise HTTPNotFound()
    except Exception:  # noqa: BLE001
        raise HTTPNotFound()


def _read_env(*keys: str, default: str = "") -> str:
    for key in keys:
        value = environ.get(key, "")
        if value is None:
            continue
        cleaned = str(value).strip().strip('"').strip("'")
        if cleaned:
            return cleaned
    return default


def _get_webchat_backend() -> str:
    backend = _read_env("WEBCHAT_BACKEND", "WEBCHAT_MODE", default="local").lower()
    if backend in {"directline", "direct-line", "copilotstudio-directline"}:
        return "directline"
    if backend in {"copilot", "copilotstudio", "remote"}:
        return "copilotstudio"
    return "local"


def _webchat_uploads_enabled() -> bool:
    return _read_env("WEBCHAT_UPLOADS_ENABLED", default="false").lower() in {"1", "true", "yes", "on"}


def _webchat_upload_button_visible() -> bool:
    return _read_env("WEBCHAT_UPLOAD_BUTTON_VISIBLE", default="false").lower() in {"1", "true", "yes", "on"}


def _get_directline_base_url() -> str:
    return _read_env("DIRECTLINE_BASE_URL", default="https://directline.botframework.com/v3/directline").rstrip("/")


def _get_webchat_public_base_url() -> str:
    public_base_url = _read_env("WEBCHAT_PUBLIC_BASE_URL")
    if not public_base_url:
        return ""
    parsed = urlparse(public_base_url)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.params
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise ValueError("WEBCHAT_PUBLIC_BASE_URL 必须是无查询参数的 HTTPS 公网地址。")
    return public_base_url.rstrip("/")


def _get_webchat_upload_retention_days() -> int:
    value = _read_env("WEBCHAT_UPLOAD_RETENTION_DAYS", default=str(DEFAULT_WEBCHAT_UPLOAD_RETENTION_DAYS))
    try:
        retention_days = int(value)
    except ValueError:
        return DEFAULT_WEBCHAT_UPLOAD_RETENTION_DAYS
    return max(0, retention_days)


def _cleanup_expired_webchat_uploads(now: float | None = None) -> int:
    if not WEBCHAT_UPLOAD_DIR.is_dir():
        return 0
    expiration_time = (time.time() if now is None else now) - _get_webchat_upload_retention_days() * 24 * 60 * 60
    deleted_count = 0
    for file_path in WEBCHAT_UPLOAD_DIR.iterdir():
        try:
            if file_path.is_file() and file_path.stat().st_mtime < expiration_time:
                file_path.unlink()
                deleted_count += 1
        except OSError:
            continue
    return deleted_count


def _local_activity(activity_type: str, text: str, from_id: str, from_name: str) -> dict[str, Any]:
    return {
        "id": str(uuid4()),
        "type": activity_type,
        "text": text,
        "from": {"id": from_id, "name": from_name},
    }


def _local_reply(text: str) -> str:
    if text.strip().lower() == "/help":
        return "Welcome to the Echo Agent sample. Type /help for help or send a message to see the echo feature in action."
    return f"you said: {text}"


def _build_activities_url(conversations_url: str, conversation_id: str) -> str:
    parsed = urlparse(conversations_url)
    query = parse_qs(parsed.query)
    api_version = query.get("api-version", ["2022-03-01-preview"])[0]
    base = conversations_url.split("/conversations", 1)[0]
    return f"{base}/conversations/{conversation_id}/activities?api-version={api_version}"


def _is_jwt_like(token: str) -> bool:
    parts = token.split(".")
    return len(parts) == 3 and all(parts)


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    try:
        payload = token.split(".")[1]
        payload += "=" * ((4 - len(payload) % 4) % 4)
        decoded = base64.urlsafe_b64decode(payload.encode("utf-8")).decode("utf-8")
        return json.loads(decoded)
    except Exception:
        return {}


def _normalize_scope(scope: str) -> str:
    normalized = scope.strip()
    if not normalized:
        return ""
    if normalized.endswith("/.default"):
        return normalized
    if normalized.startswith("http://") or normalized.startswith("https://"):
        return normalized.rstrip("/") + "/.default"
    return normalized


def _infer_scope_from_cloud() -> str:
    # Align with CopilotStudioClient scope mapping in official SDK tests.
    cloud = _read_env("COPILOT_STUDIO_CLOUD", "COPILOTSTUDIOAGENT__CLOUD", default="Prod").lower()
    custom_cloud = _read_env("COPILOT_STUDIO_CUSTOM_POWER_PLATFORM_CLOUD", "COPILOTSTUDIOAGENT__CUSTOMPOWERPLATFORMCLOUD")

    if cloud == "other" and custom_cloud:
        return _normalize_scope(f"https://{custom_cloud}" if "://" not in custom_cloud else custom_cloud)
    if cloud in {"gov", "govfr", "high", "dod"}:
        return "https://api.gov.powerplatform.microsoft.us/.default"
    if cloud == "mooncake":
        return "https://api.powerplatform.partner.microsoftonline.cn/.default"
    if cloud == "preprod":
        return "https://api.preprod.powerplatform.com/.default"

    # Prod/FirstRelease and most clouds use the public audience.
    return "https://api.powerplatform.com/.default"


async def _get_token_from_aad_client_credentials() -> str:
    tenant_id = _read_env("AAD_TENANT_ID", "COPILOTSTUDIOAGENT__TENANTID")
    client_id = _read_env("AAD_CLIENT_ID", "COPILOTSTUDIOAGENT__AGENTAPPID")
    client_secret = _read_env("AAD_CLIENT_SECRET")
    scope = _normalize_scope(_read_env("AAD_SCOPE")) or _infer_scope_from_cloud()

    if not tenant_id or not client_id or not client_secret or not scope:
        raise ValueError(
            "AAD client credentials mode requires AAD_TENANT_ID, AAD_CLIENT_ID, AAD_CLIENT_SECRET and AAD_SCOPE (or inferable scope)."
        )

    now = time.time()
    cached_token = _TOKEN_CACHE.get("access_token", "")
    cached_expiry = float(_TOKEN_CACHE.get("expires_at", 0.0))
    if cached_token and now < cached_expiry - 120:
        return str(cached_token)

    token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    form_data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "client_credentials",
        "scope": scope,
    }

    timeout = ClientTimeout(total=30)
    async with ClientSession(timeout=timeout) as session:
        async with session.post(token_url, data=form_data) as response:
            body = await response.json(content_type=None)
            if response.status != 200:
                raise RuntimeError(f"Failed to get AAD token: {body}")

            access_token = body.get("access_token", "")
            if not access_token:
                raise RuntimeError("AAD token response does not contain access_token.")

            expires_in = int(body.get("expires_in", 3600))
            _TOKEN_CACHE["access_token"] = access_token
            _TOKEN_CACHE["expires_at"] = now + expires_in
            return access_token


async def _get_copilot_auth_header() -> dict[str, str]:
    token_mode = _read_env("COPILOT_STUDIO_TOKEN_MODE", default="raw").lower()

    if token_mode == "aad_client_credentials":
        token = await _get_token_from_aad_client_credentials()
    else:
        token = _read_env("COPILOT_STUDIO_BEARER_TOKEN")

    if not token:
        return {}

    if not _is_jwt_like(token):
        raise ValueError(
            "COPILOT_STUDIO_BEARER_TOKEN is not a valid JWT format (expected header.payload.signature)."
        )

    if token_mode == "aad_client_credentials":
        claims = _decode_jwt_payload(token)
        has_roles = bool(claims.get("roles"))
        has_scp = bool(claims.get("scp"))
        if not has_roles and not has_scp:
            raise RuntimeError(
                "AAD token was issued but contains neither roles nor scopes. "
                "This usually means the app lacks API permissions/admin consent for Copilot Studio invoke, "
                "or this endpoint does not accept app-only tokens for your tenant."
            )

    return {"Authorization": f"Bearer {token}"}


async def _proxy_json_request(method: str, url: str, payload: dict[str, Any]) -> tuple[int, Any]:
    auth_header = await _get_copilot_auth_header()
    headers = {
        "Content-Type": "application/json",
        **auth_header,
    }
    timeout = ClientTimeout(total=30)
    async with ClientSession(timeout=timeout) as session:
        request_kwargs: dict[str, Any] = {"headers": headers}
        if method.upper() != "GET":
            request_kwargs["json"] = payload
        async with session.request(method, url, **request_kwargs) as upstream:
            body = await upstream.json(content_type=None)
            return upstream.status, body


async def _directline_request(
    method: str, url: str, token: str, payload: dict[str, Any] | None = None
) -> tuple[int, Any]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    timeout = ClientTimeout(total=30)
    async with ClientSession(timeout=timeout) as session:
        request_kwargs: dict[str, Any] = {"headers": headers}
        if payload is not None and method.upper() != "GET":
            request_kwargs["json"] = payload
        async with session.request(method, url, **request_kwargs) as upstream:
            if upstream.content_length == 0:
                body: Any = {}
            else:
                try:
                    body = await upstream.json(content_type=None)
                except Exception:
                    body = {"message": await upstream.text()}
            return upstream.status, body


async def _get_directline_token() -> dict[str, Any]:
    endpoint = _read_env("COPILOT_STUDIO_DIRECTLINE_TOKEN_ENDPOINT", "DIRECTLINE_TOKEN_ENDPOINT")
    if not endpoint:
        raise ValueError(
            "Missing COPILOT_STUDIO_DIRECTLINE_TOKEN_ENDPOINT. Copy it from Copilot Studio > Channels > Mobile app > Token endpoint."
        )

    timeout = ClientTimeout(total=30)
    async with ClientSession(timeout=timeout) as session:
        async with session.get(endpoint) as response:
            body = await response.json(content_type=None)
            if response.status != 200:
                raise RuntimeError(f"Failed to get Direct Line token: {body}")
            token = body.get("token")
            if not token:
                raise RuntimeError(f"Direct Line token endpoint did not return token: {body}")
            return body


async def _start_directline_conversation() -> dict[str, Any]:
    token_response = await _get_directline_token()
    token = token_response["token"]
    expires_in = int(token_response.get("expires_in", 3600))
    start_url = f"{_get_directline_base_url()}/conversations"
    status, body = await _directline_request("POST", start_url, token, {})
    if status >= 400:
        raise RuntimeError(f"Failed to start Direct Line conversation: {body}")
    conversation_id = body.get("conversationId") or body.get("conversation_id")
    token = body.get("token") or token

    if not conversation_id:
        raise RuntimeError(f"Direct Line did not return conversationId: {body}")

    _DIRECTLINE_CONVERSATIONS[conversation_id] = {
        "token": token,
        "expires_at": time.time() + expires_in,
    }
    return {"conversationId": conversation_id, "watermark": ""}


def _get_directline_conversation(conversation_id: str) -> dict[str, Any]:
    conversation = _DIRECTLINE_CONVERSATIONS.get(conversation_id)
    if not conversation:
        raise ValueError("Direct Line conversation was not found or server was restarted. Start a new conversation.")
    if float(conversation.get("expires_at", 0)) <= time.time():
        _DIRECTLINE_CONVERSATIONS.pop(conversation_id, None)
        raise ValueError("Direct Line token expired. Start a new conversation.")
    return conversation


async def _webchat_page(request: Request) -> Response:
    user, status, denial_reason = _approved_webchat_user(request)
    if user is None:
        return Response(
            text=_WEBCHAT_LOGIN_HTML,
            content_type="text/html",
        )
    if user == {} and (_entra_enabled() or _entra_china_enabled()):
        template = _TEMPLATE_ENV.get_template("webchat_pending.html")
        return Response(text=template.render(status=status, denial_reason=denial_reason), content_type="text/html")
    upload_button_hidden = "" if _webchat_upload_button_visible() else "hidden"
    return Response(
        text=_WEBCHAT_HTML.replace("__WEBCHAT_UPLOAD_BUTTON_HIDDEN__", upload_button_hidden),
        content_type="text/html",
    )


async def _webchat_superpop_logo(_: Request) -> Response:
    asset_path = TEMPLATES_DIR / "superpop.png"
    if not asset_path.is_file():
        return Response(status=404)
    return FileResponse(asset_path)


async def _webchat_config(request: Request) -> Response:
    denied = _webchat_access_denied(request)
    if denied is not None:
        return denied
    backend = _get_webchat_backend()
    conversations_url = _read_env("COPILOT_STUDIO_CONVERSATIONS_URL")
    token_mode = _read_env("COPILOT_STUDIO_TOKEN_MODE", default="raw").lower()
    effective_scope = _normalize_scope(_read_env("AAD_SCOPE")) or _infer_scope_from_cloud()
    return json_response(
        {
            "backendMode": backend,
            "conversationsUrl": conversations_url,
            "hasDirectLineTokenEndpoint": bool(
                _read_env("COPILOT_STUDIO_DIRECTLINE_TOKEN_ENDPOINT", "DIRECTLINE_TOKEN_ENDPOINT")
            ),
            "hasBearerToken": bool(_read_env("COPILOT_STUDIO_BEARER_TOKEN")),
            "tokenMode": token_mode,
            "hasAadClientCredentials": bool(
                _read_env("AAD_TENANT_ID", "COPILOTSTUDIOAGENT__TENANTID")
                and _read_env("AAD_CLIENT_ID", "COPILOTSTUDIOAGENT__AGENTAPPID")
                and environ.get("AAD_CLIENT_SECRET", "").strip()
            ),
            "aadScope": effective_scope,
            "effectiveScope": effective_scope,
            "userId": environ.get("WEBCHAT_USER_ID", "web-user"),
            "userName": environ.get("WEBCHAT_USER_NAME", "Web User"),
        }
    )


async def _webchat_start(req: Request) -> Response:
    denied = _webchat_access_denied(req)
    if denied is not None:
        return denied
    if _get_webchat_backend() == "local":
        conversation_id = str(uuid4())
        _LOCAL_CONVERSATIONS[conversation_id] = [
            _local_activity(
                "message",
                "Welcome to the Echo Agent sample. Type /help for help or send a message to see the echo feature in action.",
                "local-agent",
                "Echo Agent",
            )
        ]
        return json_response({"conversationId": conversation_id, "watermark": "0"})

    if _get_webchat_backend() == "directline":
        try:
            return json_response(await _start_directline_conversation())
        except ValueError as error:
            return json_response({"error": str(error)}, status=400)
        except RuntimeError as error:
            return json_response({"error": str(error)}, status=502)

    conversations_url = _read_env("COPILOT_STUDIO_CONVERSATIONS_URL")
    if not conversations_url:
        return json_response(
            {
                "error": "Missing COPILOT_STUDIO_CONVERSATIONS_URL."
            },
            status=400,
        )
    if "/authenticated/" in conversations_url.lower():
        return json_response(
            {
                "error": "This Copilot Studio URL is an authenticated endpoint. It cannot be used for anonymous web access. Use WEBCHAT_BACKEND=directline with COPILOT_STUDIO_DIRECTLINE_TOKEN_ENDPOINT, or use the No authentication Web Chat embed code."
            },
            status=400,
        )

    payload = await req.json() if req.can_read_body else {}
    try:
        status, body = await _proxy_json_request("POST", conversations_url, payload)
        return json_response(body, status=status)
    except ValueError as error:
        return json_response({"error": str(error)}, status=400)
    except RuntimeError as error:
        return json_response({"error": str(error)}, status=502)


async def _webchat_send(req: Request) -> Response:
    denied = _webchat_access_denied(req)
    if denied is not None:
        return denied
    payload = await req.json()
    conversation_id = payload.get("conversationId", "").strip()
    text = payload.get("text", "")
    attachments = payload.get("attachments", [])
    user_id = payload.get("userId", "web-user")
    user_name = payload.get("userName", "Web User")

    if not conversation_id:
        return json_response({"error": "conversationId is required."}, status=400)
    if not isinstance(attachments, list) or len(attachments) > 5:
        return json_response({"error": "attachments must contain at most 5 files."}, status=400)
    attachment_names = [str(item.get("name", "")).strip() for item in attachments if isinstance(item, dict)]
    attachment_names = [name for name in attachment_names if name]
    if not text and not attachment_names:
        return json_response({"error": "text or attachments is required."}, status=400)
    if not text:
        text = f"已上传附件：{', '.join(attachment_names)}"

    # 记录前端用户提交问题信息，用于后台日志查询。
    record_webchat_question(req, text, _entra_login_account(_entra_get_session(req)))

    if _get_webchat_backend() == "local":
        activities = _LOCAL_CONVERSATIONS.setdefault(conversation_id, [])
        activities.append(_local_activity("message", text, user_id, user_name))
        activities.append(_local_activity("message", _local_reply(text), "local-agent", "Echo Agent"))
        return json_response({"id": activities[-1]["id"]})

    if _get_webchat_backend() == "directline":
        try:
            conversation = _get_directline_conversation(conversation_id)
        except ValueError as error:
            return json_response({"error": str(error)}, status=400)

        send_url = f"{_get_directline_base_url()}/conversations/{conversation_id}/activities"
        activity = {
            "type": "message",
            "text": text,
            "from": {"id": user_id, "name": user_name},
            "locale": "zh-CN",
        }
        if attachment_names:
            activity["attachments"] = [
                {
                    "contentType": str(item.get("contentType", "application/octet-stream")),
                    "contentUrl": _resolve_attachment_for_copilot(
                        str(item.get("contentUrl", "")),
                        str(item.get("contentType", "application/octet-stream")),
                    ),
                    "name": str(item.get("name", "")),
                }
                for item in attachments
                if isinstance(item, dict) and str(item.get("name", "")).strip()
            ]
        status, body = await _directline_request("POST", send_url, str(conversation["token"]), activity)
        return json_response(body, status=status)

    conversations_url = _read_env("COPILOT_STUDIO_CONVERSATIONS_URL")
    if not conversations_url:
        return json_response({"error": "Missing COPILOT_STUDIO_CONVERSATIONS_URL."}, status=400)
    if "/authenticated/" in conversations_url.lower():
        return json_response(
            {"error": "Authenticated Copilot Studio endpoint cannot be used for anonymous web access. Use Direct Line token endpoint."},
            status=400,
        )

    send_url = _build_activities_url(conversations_url, conversation_id)
    activity = {
        "type": "message",
        "text": text,
        "from": {"id": user_id, "name": user_name},
        "locale": "zh-CN",
    }
    if attachment_names:
        activity["attachments"] = [
            {
                "contentType": str(item.get("contentType", "application/octet-stream")),
                "contentUrl": _resolve_attachment_for_copilot(
                    str(item.get("contentUrl", "")),
                    str(item.get("contentType", "application/octet-stream")),
                ),
                "name": str(item.get("name", "")),
            }
            for item in attachments
            if isinstance(item, dict) and str(item.get("name", "")).strip()
        ]

    try:
        status, body = await _proxy_json_request("POST", send_url, activity)
        return json_response(body, status=status)
    except ValueError as error:
        return json_response({"error": str(error)}, status=400)
    except RuntimeError as error:
        return json_response({"error": str(error)}, status=502)


# Base64 data-URL size threshold: images up to 1 MB are inlined to avoid
# Copilot Studio being unable to reach the public URL (network isolation,
# firewall rules, or Cache-Control mismatches).
_MAX_INLINE_BYTES = 1 * 1024 * 1024


def _resolve_attachment_for_copilot(content_url: str, content_type: str) -> str:
    """Return a base64 data URL for locally-stored upload images (≤1 MB).

    Copilot Studio's backend servers fetch the contentUrl to pass the image
    to the AI model.  If our server is behind a firewall or the DNS is not
    reachable from Microsoft's cloud, the fetch fails and Copilot Studio
    returns a generic SystemError.  Inlining the image eliminates that
    external dependency entirely.
    """
    if not content_type.startswith("image/"):
        return content_url
    try:
        filename = Path(urlparse(content_url).path).name
        if not filename:
            return content_url
        file_path = WEBCHAT_UPLOAD_DIR / filename
        if not file_path.is_file():
            return content_url
        raw = file_path.read_bytes()
        if len(raw) > _MAX_INLINE_BYTES:
            return content_url
        return f"data:{content_type};base64,{base64.b64encode(raw).decode()}"
    except Exception:
        return content_url


def _uploaded_file_type(filename: str, content: bytes) -> tuple[str, str] | None:
    suffix = Path(filename).suffix.lower()
    image_types = {
        ".png": (b"\x89PNG\r\n\x1a\n", "image/png"),
        ".jpg": (b"\xff\xd8\xff", "image/jpeg"),
        ".jpeg": (b"\xff\xd8\xff", "image/jpeg"),
        ".webp": (b"RIFF", "image/webp"),
    }
    if suffix in image_types:
        signature, content_type = image_types[suffix]
        if content.startswith(signature) and (suffix != ".webp" or content[8:12] == b"WEBP"):
            return suffix, content_type
        return None
    if suffix not in {".log", ".txt"}:
        return None
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if b"\x00" in content:
        return None
    return suffix, "text/plain"


async def _webchat_upload(req: Request) -> Response:
    denied = _webchat_access_denied(req)
    if denied is not None:
        return denied
    if not _webchat_uploads_enabled():
        return json_response({"error": "当前未启用文件上传功能。"}, status=403)
    reader = await req.multipart()
    field = await reader.next()
    if field is None or field.name != "file" or not field.filename:
        return json_response({"error": "file is required."}, status=400)
    content = await field.read(decode=False)
    if not content:
        return json_response({"error": "文件不能为空。"}, status=400)
    if len(content) > MAX_UPLOAD_SIZE:
        return json_response({"error": "文件不能超过 5 MB。"}, status=413)
    detected = _uploaded_file_type(field.filename, content)
    if detected is None:
        return json_response({"error": "仅支持 PNG、JPG、WEBP 图片以及 TXT、LOG 日志文件。"}, status=415)
    suffix, content_type = detected
    WEBCHAT_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    upload_id = str(uuid4())
    (WEBCHAT_UPLOAD_DIR / f"{upload_id}{suffix}").write_bytes(content)
    # #region debug-point A:upload-public-url
    _debug_url = _read_env("DEBUG_SERVER_URL"); _debug_session = _read_env("DEBUG_SESSION_ID", default="image-upload-system-error"); _public_base_url = _get_webchat_public_base_url(); _public_url = f"{_public_base_url}/webchat/uploads/{upload_id}{suffix}" if _public_base_url else str(req.url.with_path(f"/webchat/uploads/{upload_id}{suffix}").with_query(""));
    if _debug_url:
        try:
            async with ClientSession(timeout=ClientTimeout(total=1)) as _debug_client:
                await _debug_client.post(_debug_url, json={"sessionId": _debug_session, "runId": "post-fix", "hypothesisId": "A", "location": "start_server.py:_webchat_upload", "msg": "[DEBUG] 已生成上传附件公开地址", "data": {"scheme": urlparse(_public_url).scheme, "host": urlparse(_public_url).netloc, "path_prefix": "/webchat/uploads/" in _public_url, "uses_configured_public_base_url": bool(_public_base_url)}})
        except Exception:
            pass
    # #endregion
    return json_response(
        {
            "id": upload_id,
            "name": Path(field.filename).name[:160],
            "contentType": content_type,
            "size": len(content),
            "contentUrl": _public_url,
        }
    )


async def _webchat_uploaded_file(req: Request) -> Response:
    filename = Path(req.match_info["filename"]).name
    if filename != req.match_info["filename"] or not filename:
        raise HTTPNotFound()
    file_path = WEBCHAT_UPLOAD_DIR / filename
    if not file_path.is_file():
        raise HTTPNotFound()
    return FileResponse(file_path, headers={"Cache-Control": "public, max-age=300"})


async def _webchat_poll(req: Request) -> Response:
    denied = _webchat_access_denied(req)
    if denied is not None:
        return denied
    payload = await req.json()
    conversation_id = payload.get("conversationId", "").strip()
    watermark = payload.get("watermark", "")
    if not conversation_id:
        return json_response({"error": "conversationId is required."}, status=400)

    if _get_webchat_backend() == "local":
        activities = _LOCAL_CONVERSATIONS.setdefault(conversation_id, [])
        try:
            start_index = int(watermark) if watermark else 0
        except ValueError:
            start_index = 0
        next_activities = activities[start_index:]
        return json_response({"activities": next_activities, "watermark": str(len(activities))})

    if _get_webchat_backend() == "directline":
        try:
            conversation = _get_directline_conversation(conversation_id)
        except ValueError as error:
            return json_response({"error": str(error)}, status=400)

        poll_url = f"{_get_directline_base_url()}/conversations/{conversation_id}/activities"
        if watermark:
            poll_url = f"{poll_url}?watermark={watermark}"
        status, body = await _directline_request("GET", poll_url, str(conversation["token"]))
        return json_response(body, status=status)

    conversations_url = _read_env("COPILOT_STUDIO_CONVERSATIONS_URL")
    if not conversations_url:
        return json_response({"error": "Missing COPILOT_STUDIO_CONVERSATIONS_URL."}, status=400)
    if "/authenticated/" in conversations_url.lower():
        return json_response(
            {"error": "Authenticated Copilot Studio endpoint cannot be used for anonymous web access. Use Direct Line token endpoint."},
            status=400,
        )

    activities_url = _build_activities_url(conversations_url, conversation_id)
    if watermark:
        activities_url = f"{activities_url}&watermark={watermark}"

    try:
        status, body = await _proxy_json_request("GET", activities_url, {})
        return json_response(body, status=status)
    except ValueError as error:
        return json_response({"error": str(error)}, status=400)
    except RuntimeError as error:
        return json_response({"error": str(error)}, status=502)


def create_application(
    agent_application: AgentApplication,
    auth_configuration: AgentAuthConfiguration | None,
) -> Application:
    async def entry_point(req: Request) -> Response:
        agent: AgentApplication = req.app["agent_app"]
        adapter: CloudAdapter = req.app["adapter"]
        response = await start_agent_process(req, agent, adapter)
        return response or Response(status=202)

    async def health(_: Request) -> Response:
        return Response(status=200)

    async def faq_detail_with_login_account(req: Request) -> Response:
        user, _status, _denial_reason = _approved_webchat_user(req)
        if user is None:
            raise HTTPFound("/webchat")
        if user == {}:
            return await _webchat_page(req)
        if user.get("anonymous"):
            return Response(status=503, text="FAQ 详情需要配置并完成 Entra ID 身份验证")
        req["login_account"] = _entra_login_account(_entra_get_session(req))
        return await faq_detail(req)

    # 在应用启动时幂等初始化 FAQ 数据表与索引。
    initialize_database()
    _cleanup_expired_webchat_uploads()
    effective_auth_configuration = auth_configuration or AgentAuthConfiguration(anonymous_allowed=True)
    app = Application()
    agent_app = Application(middlewares=[jwt_authorization_middleware])
    agent_app.router.add_post("/messages", entry_point)
    agent_app.router.add_get("/messages", health)

    agent_app["agent_configuration"] = effective_auth_configuration
    agent_app["agent_app"] = agent_application
    agent_app["adapter"] = agent_application.adapter

    app.add_subapp("/api", agent_app)
    app.router.add_get("/entra/login", _entra_login_route)
    app.router.add_get("/entra/china/login", _entra_china_login_route)
    app.router.add_get("/entra/callback", _entra_callback_route)
    app.router.add_post("/entra/callback", _entra_callback_route)
    app.router.add_get("/entra/china/callback", _entra_china_callback_route)
    app.router.add_post("/entra/china/callback", _entra_china_callback_route)
    app.router.add_get("/entra/logout", _entra_logout_route)
    app.router.add_post("/entra/logout", _entra_logout_route)
    app.router.add_get("/entra/auth-status", _entra_auth_status_route)
    app.router.add_get("/entra/photo", _entra_photo_route)
    app.router.add_get("/webchat", _webchat_page)
    app.router.add_get("/webchat/superpop.png", _webchat_superpop_logo)
    app.router.add_get("/templates/superpop.png", _webchat_superpop_logo)
    app.router.add_get("/webchat/config", _webchat_config)
    app.router.add_post("/webchat/start", _webchat_start)
    app.router.add_post("/webchat/send", _webchat_send)
    app.router.add_post("/webchat/upload", _webchat_upload)
    app.router.add_get("/webchat/uploads/{filename}", _webchat_uploaded_file)
    app.router.add_post("/webchat/poll", _webchat_poll)
    # FAQ 目录接口与详情页独立于既有聊天接口，确保聊天功能保持不变。
    app.router.add_get("/faq/directory", faq_directory)
    app.router.add_get("/faq/images/{filename}", faq_image)
    app.router.add_get("/faq/{faq_id}", faq_detail_with_login_account)
    app.router.add_get("/admin", admin_dashboard)
    app.router.add_post("/admin", admin_dashboard)
    app.router.add_get("/admin/login", admin_login)
    app.router.add_post("/admin/login", admin_login)
    app.router.add_get("/admin/login-method", admin_login_method)
    app.router.add_get("/admin/login/captcha", admin_login_captcha)
    app.router.add_get("/admin/logout", admin_logout)
    app.router.add_get("/admin/faq", admin_list)
    app.router.add_get("/admin/accounts", admin_accounts_list)
    app.router.add_post("/admin/accounts", admin_accounts_create)
    app.router.add_put("/admin/accounts/{admin_id}", admin_accounts_update)
    app.router.add_post("/admin/accounts/{admin_id}/password", admin_accounts_change_password)
    app.router.add_get("/admin/accounts/{admin_id}/mfa/setup", admin_mfa_setup)
    app.router.add_post("/admin/accounts/{admin_id}/mfa/enable", admin_mfa_enable)
    app.router.add_post("/admin/accounts/{admin_id}/mfa/disable", admin_mfa_disable)
    app.router.add_delete("/admin/accounts/{admin_id}", admin_accounts_delete)
    app.router.add_get("/admin/logs", admin_logs)
    app.router.add_get("/admin/connection-status", _admin_connection_status)
    app.router.add_get("/admin/webchat-access", admin_webchat_access_list)
    app.router.add_post("/admin/webchat-access", admin_webchat_access_update)
    app.router.add_get("/admin/faq/new", admin_new)
    app.router.add_post("/admin/faq/new", admin_new)
    app.router.add_post("/admin/faq/import-content", admin_faq_import_content)
    app.router.add_post("/admin/faq/images", admin_faq_image_upload)
    app.router.add_get("/admin/faq/{faq_id}/edit", admin_edit)
    app.router.add_post("/admin/faq/{faq_id}/edit", admin_edit)
    app.router.add_post("/admin/faq/{faq_id}/delete", admin_delete)
    # 根路径保留原有聊天入口，防止影响已有访问地址。
    app.router.add_get("/", _webchat_page)

    return app


def start_server(
    agent_application: AgentApplication,
    auth_configuration: AgentAuthConfiguration | None,
) -> None:
    run_app(create_application(agent_application, auth_configuration), host="0.0.0.0", port=5300)
