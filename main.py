import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, Dict, Mapping

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("ai_backend")
logging.basicConfig(level=logging.INFO)

DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "gpt-5.4")

# 建议通过环境变量配置你的网关地址
# 例如：https://api.example.com
NEW_API_BASE_URL = os.getenv("NEW_API_BASE_URL", "https://api.apiyi.com").rstrip("/")
NEW_API_KEY = os.getenv("NEW_API_KEY", "")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 全局复用连接，适合流式转发
    app.state.http_client = httpx.AsyncClient(
        timeout=None,
        trust_env=False,  # 不使用系统代理环境变量，降低意外风险
    )
    try:
        yield
    finally:
        await app.state.http_client.aclose()


app = FastAPI(
    title="AI Proxy Backend",
    version="1.0.0",
    docs_url=None,      # 禁用 /docs
    redoc_url=None,     # 禁用 /redoc
    openapi_url=None,   # 同时禁用 openapi.json，进一步降低暴露面
    lifespan=lifespan,
)


def _safe_headers(headers: Mapping[str, str]) -> Dict[str, str]:
    """
    清理响应头，移除危险或不必要的头。
    """
    blocked = {
        "server",
        "x-powered-by",
        "content-length",
        "connection",
        "keep-alive",
        "transfer-encoding",
        "upgrade",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
    }
    return {k: v for k, v in headers.items() if k.lower() not in blocked}


def _build_upstream_headers(is_stream: bool) -> Dict[str, str]:
    if not NEW_API_KEY:
        # 这里不直接抛 Python 堆栈，交给接口层做安全返回
        raise ValueError("NEW_API_KEY is not configured")

    return {
        "Authorization": f"Bearer {NEW_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if is_stream else "application/json",
    }


def _generic_error(message: str, status_code: int = 500) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"message": message}},
    )


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    """
    安全中间件：
    1. 强制删除响应头中的 Server / X-Powered-By
    2. 捕获未处理异常，避免向前端暴露 Python 堆栈
    """
    try:
        response = await call_next(request)
    except Exception:
        logger.exception("Unhandled exception")
        response = _generic_error("Internal server error", 500)

    # 删除敏感/多余响应头
    try:
        for header_name in ("server", "x-powered-by"):
            if header_name in response.headers:
                del response.headers[header_name]
    except Exception:
        # 不让头处理逻辑影响主流程
        pass

    return response


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    # 统一错误格式，不输出 traceback
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"message": str(exc.detail)}},
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    # 不返回内部校验细节堆栈
    return JSONResponse(
        status_code=422,
        content={"error": {"message": "Invalid request"}}
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception")
    return _generic_error("Internal server error", 500)


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """
    OpenAI 兼容接口：
    - 接收前端请求
    - 转发到 NEW_API_BASE_URL/v1/chat/completions
    - 支持 stream=true 的 SSE 流式输出
    - 默认模型固定为 gpt-5.4-mini
    """
    if not NEW_API_KEY:
        return _generic_error("Server misconfigured: NEW_API_KEY is missing", 500)

    try:
        payload = await request.json()
    except Exception:
        return _generic_error("Invalid JSON body", 400)

    if not isinstance(payload, dict):
        return _generic_error("Request body must be a JSON object", 400)

    # 模型限制：强制固定为 gpt-5.4-mini
    payload["model"] = DEFAULT_MODEL

    is_stream = bool(payload.get("stream", False))
    upstream_url = f"{NEW_API_BASE_URL}/v1/chat/completions"

    client: httpx.AsyncClient = request.app.state.http_client
    headers = _build_upstream_headers(is_stream=is_stream)

    # 使用 build_request + send(stream=True) 方便我们同时支持：
    # 1) 先检查上游状态码
    # 2) 再决定是流式转发还是直接返回错误
    upstream_request = client.build_request(
        method="POST",
        url=upstream_url,
        headers=headers,
        json=payload,
    )

    upstream_response = await client.send(upstream_request, stream=True)

    # 上游报错时，直接返回上游内容，但不暴露 Python 堆栈
    if upstream_response.status_code >= 400:
        content = await upstream_response.aread()
        media_type = upstream_response.headers.get("content-type", "application/json")
        await upstream_response.aclose()
        return Response(
            content=content,
            status_code=upstream_response.status_code,
            media_type=media_type,
            headers=_safe_headers(upstream_response.headers),
        )

    if is_stream:
        async def stream_generator():
            try:
                async for chunk in upstream_response.aiter_raw():
                    yield chunk
            finally:
                await upstream_response.aclose()

        return StreamingResponse(
            stream_generator(),
            status_code=upstream_response.status_code,
            media_type=upstream_response.headers.get(
                "content-type",
                "text/event-stream",
            ),
            headers=_safe_headers(upstream_response.headers),
        )

    # 非流式：直接把上游响应透传回去
    content = await upstream_response.aread()
    media_type = upstream_response.headers.get("content-type", "application/json")
    await upstream_response.aclose()

    # 如果是 JSON，尽量按 JSONResponse 返回
    if "application/json" in media_type.lower():
        try:
            data = json.loads(content.decode("utf-8"))
            return JSONResponse(
                content=data,
                status_code=upstream_response.status_code,
                headers=_safe_headers(upstream_response.headers),
            )
        except Exception:
            pass

    return Response(
        content=content,
        status_code=upstream_response.status_code,
        media_type=media_type,
        headers=_safe_headers(upstream_response.headers),
    )
