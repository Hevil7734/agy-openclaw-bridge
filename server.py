import asyncio
import json
import logging
import os
import time
import uuid
from typing import Dict, Any, Optional

from aiohttp import web

import config
from agy_runner import (
    format_messages_to_prompt,
    resolve_model,
    run_agy_non_stream,
    run_agy_stream,
    GLOBAL_POOL,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("agy_api_server")

# In-memory session tracking: maps user/session keys to agy conversation IDs
SESSION_MAP: Dict[str, str] = {}

def get_auth_error(message: str = "Invalid API key") -> web.Response:
    return web.json_response(
        {
            "error": {
                "message": message,
                "type": "invalid_request_error",
                "code": "invalid_api_key"
            }
        },
        status=401,
        headers={"Access-Control-Allow-Origin": "*"}
    )

@web.middleware
async def cors_and_auth_middleware(request: web.Request, handler):
    # Handle preflight CORS OPTIONS requests immediately
    if request.method == "OPTIONS":
        return web.Response(
            status=204,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Conversation-Id, X-Session-Id, X-Agy-Worker",
                "Access-Control-Max-Age": "86400",
            }
        )

    # Check API key if configured
    if config.API_KEY and request.path not in ("/", "/health", "/v1/health"):
        auth_header = request.headers.get("Authorization", "")
        token = ""
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
        elif auth_header:
            token = auth_header.strip()

        if token != config.API_KEY:
            logger.warning("Unauthorized access attempt to %s", request.path)
            return get_auth_error()

    response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response

async def handle_root(request: web.Request) -> web.Response:
    """Root status endpoint with worker pool telemetry."""
    return web.json_response({
        "service": "agy-openclaw-bridge",
        "description": "OpenAI-compatible Multi-Worker Gateway for Google Antigravity (AGY) CLI",
        "status": "online",
        "version": "2.0.0",
        "default_model": config.DEFAULT_MODEL,
        "pool": GLOBAL_POOL.get_status(),
        "endpoints": [
            "GET  /v1/models",
            "GET  /v1/models/{model_id}",
            "POST /v1/chat/completions",
            "POST /v1/completions",
            "GET  /health",
        ]
    })

async def handle_health(request: web.Request) -> web.Response:
    """Health check endpoint reporting live worker load."""
    return web.json_response({
        "status": "healthy",
        "pool": GLOBAL_POOL.get_status(),
        "active_sessions": len(SESSION_MAP)
    })

async def handle_list_models(request: web.Request) -> web.Response:
    """OpenAI-compatible GET /v1/models endpoint."""
    models_data = []
    created_time = int(time.time())

    for m in config.KNOWN_MODELS:
        models_data.append({
            "id": m["id"],
            "object": "model",
            "created": created_time,
            "owned_by": "antigravity",
            "permission": [],
            "root": m["id"],
            "parent": None,
        })

    # Add worker pool aliases and per-worker models for targeted routing
    for w in config.DEFAULT_WORKERS:
        models_data.append({
            "id": w["id"],
            "object": "model",
            "created": created_time,
            "owned_by": f"antigravity-worker-{w['id']}",
            "permission": [],
            "root": w["id"],
            "parent": None,
        })
        for base in ["gemini-3.8-flash-high", "gemini-3.7-flash-high", "claude-sonnet-4-6"]:
            models_data.append({
                "id": f"{base}@{w['id']}",
                "object": "model",
                "created": created_time,
                "owned_by": f"antigravity-worker-{w['id']}",
                "permission": [],
                "root": base,
                "parent": None,
            })

    return web.json_response({
        "object": "list",
        "data": models_data
    })

async def handle_retrieve_model(request: web.Request) -> web.Response:
    """OpenAI-compatible GET /v1/models/{model_id} endpoint."""
    model_id = request.match_info.get("model_id", "")
    resolved = resolve_model(model_id)

    matched = next((m for m in config.KNOWN_MODELS if m["id"] == resolved), None)
    if not matched:
        # Accept dynamic model ID
        matched = {"id": resolved, "name": resolved}

    return web.json_response({
        "id": matched["id"],
        "object": "model",
        "created": int(time.time()),
        "owned_by": "antigravity",
        "permission": [],
        "root": matched["id"],
        "parent": None,
    })

async def handle_chat_completions(request: web.Request) -> web.StreamResponse:
    """OpenAI-compatible POST /v1/chat/completions endpoint."""
    try:
        body = await request.json()
    except Exception as e:
        return web.json_response(
            {"error": {"message": f"Invalid JSON body: {str(e)}", "type": "invalid_request_error"}},
            status=400
        )

    messages = body.get("messages", [])
    if not messages:
        return web.json_response(
            {"error": {"message": "'messages' array is required and cannot be empty", "type": "invalid_request_error"}},
            status=400
        )

    requested_model = body.get("model", config.DEFAULT_MODEL)

    preferred_worker = request.headers.get("X-Agy-Worker") or body.get("worker")
    if not preferred_worker and "@" in requested_model:
        requested_model, preferred_worker = requested_model.split("@", 1)

    resolved_model = resolve_model(requested_model)
    stream = bool(body.get("stream", False))
    effort = body.get("effort") or body.get("reasoning_effort")

    # --- Conversation Tracking & Anti-Bloat Policy ---
    conv_id = request.headers.get("X-Conversation-Id") or body.get("conversation_id")
    user_id = body.get("user") or request.headers.get("X-Session-Id")

    persist_flag = body.get("persist")
    if persist_flag is None:
        h_persist = request.headers.get("X-Persist", "").lower()
        if h_persist in ("true", "1", "yes"):
            persist_flag = True
        elif h_persist in ("false", "0", "no"):
            persist_flag = False

    ephemeral_flag = body.get("ephemeral")
    if ephemeral_flag is None:
        h_eph = request.headers.get("X-Ephemeral", "").lower()
        if h_eph in ("true", "1", "yes"):
            ephemeral_flag = True

    continuation_only = False
    should_cleanup = False

    if config.CONVERSATION_MODE == "session":
        session_key = user_id or config.DEFAULT_SESSION_NAME
        if not conv_id and session_key in SESSION_MAP:
            conv_id = SESSION_MAP[session_key]
            continuation_only = True
        should_cleanup = True if ephemeral_flag else False
    elif config.CONVERSATION_MODE == "ephemeral":
        # By default in ephemeral mode, every request cleans up its conversation artifacts
        # unless explicit persistence is requested
        if persist_flag:
            should_cleanup = False
        elif conv_id and not ephemeral_flag:
            should_cleanup = False
            continuation_only = True
        else:
            should_cleanup = True
    else:  # "persist" mode
        should_cleanup = True if ephemeral_flag else False

    prompt = format_messages_to_prompt(messages, continuation_only=continuation_only)
    logger.info(
        "Request for model=%s (worker=%s, stream=%s, conv=%s, user=%s, mode=%s, cleanup=%s)",
        resolved_model, preferred_worker or "auto-pool", stream, conv_id, user_id, config.CONVERSATION_MODE, should_cleanup
    )

    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created_ts = int(time.time())

    # --- NON-STREAMING RESPONSE ---
    if not stream:
        try:
            agy_res = await run_agy_non_stream(
                prompt=prompt,
                model=resolved_model,
                conversation_id=conv_id,
                effort=effort,
                preferred_worker=preferred_worker,
            )
            
            worker_id = agy_res.get("worker", "agy1")
            worker_data_dir = agy_res.get("worker_data_dir")
            new_conv_id = agy_res.get("conversation_id", conv_id or "")

            if user_id and new_conv_id and not should_cleanup:
                SESSION_MAP[user_id] = new_conv_id
            elif config.CONVERSATION_MODE == "session" and new_conv_id:
                SESSION_MAP[config.DEFAULT_SESSION_NAME] = new_conv_id

            response_text = agy_res.get("response", "")
            usage_info = agy_res.get("usage", {})

            # If ephemeral cleanup is active, remove temporary conversation artifacts
            if should_cleanup and new_conv_id:
                from agy_runner import cleanup_conversation
                cleanup_conversation(new_conv_id, data_dir=worker_data_dir)

            res_payload = {
                "id": completion_id,
                "object": "chat.completion",
                "created": created_ts,
                "model": resolved_model,
                "worker": worker_id,
                "system_fingerprint": f"fp_antigravity_{worker_id}",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": response_text
                        },
                        "logprobs": None,
                        "finish_reason": "stop"
                    }
                ],
                "usage": {
                    "prompt_tokens": usage_info.get("input_tokens", 0),
                    "completion_tokens": usage_info.get("output_tokens", 0),
                    "total_tokens": usage_info.get("total_tokens", 0)
                },
                "conversation_id": "" if should_cleanup else new_conv_id
            }

            headers = {"X-Agy-Worker": worker_id}
            if not should_cleanup and new_conv_id:
                headers["X-Conversation-Id"] = new_conv_id

            return web.json_response(res_payload, headers=headers)

        except Exception as ex:
            logger.exception("Error processing chat completion: %s", ex)
            return web.json_response(
                {
                    "error": {
                        "message": str(ex),
                        "type": "api_error",
                        "code": "internal_error"
                    }
                },
                status=500
            )

    # --- STREAMING RESPONSE (SSE) ---
    sse_response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream; charset=utf-8",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
        }
    )
    await sse_response.prepare(request)

    # Send initial chunk with empty assistant role
    initial_chunk = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created_ts,
        "model": resolved_model,
        "system_fingerprint": "fp_antigravity",
        "choices": [
            {
                "index": 0,
                "delta": {
                    "role": "assistant",
                    "content": ""
                },
                "logprobs": None,
                "finish_reason": None
            }
        ]
    }
    await sse_response.write(f"data: {json.dumps(initial_chunk)}\n\n".encode("utf-8"))

    final_conv_id = conv_id or ""
    active_worker_id = preferred_worker or ""
    active_worker_data_dir = None

    try:
        async for event in run_agy_stream(
            prompt=prompt,
            model=resolved_model,
            conversation_id=conv_id,
            effort=effort,
            preferred_worker=preferred_worker,
        ):
            if "worker" in event:
                active_worker_id = event["worker"]
            if "worker_data_dir" in event:
                active_worker_data_dir = event["worker_data_dir"]

            event_type = event.get("type")
            if event_type == "init":
                final_conv_id = event.get("conversation_id", final_conv_id)
                if user_id and final_conv_id:
                    SESSION_MAP[user_id] = final_conv_id

            elif event_type == "delta":
                delta_chunk = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created_ts,
                    "model": resolved_model,
                    "worker": active_worker_id,
                    "system_fingerprint": f"fp_antigravity_{active_worker_id}",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "content": event.get("content", "")
                            },
                            "logprobs": None,
                            "finish_reason": None
                        }
                    ]
                }
                await sse_response.write(f"data: {json.dumps(delta_chunk)}\n\n".encode("utf-8"))

            elif event_type == "finish":
                final_conv_id = event.get("conversation_id", final_conv_id)
                if user_id and final_conv_id and not should_cleanup:
                    SESSION_MAP[user_id] = final_conv_id
                elif config.CONVERSATION_MODE == "session" and final_conv_id:
                    SESSION_MAP[config.DEFAULT_SESSION_NAME] = final_conv_id
                
                finish_chunk = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created_ts,
                    "model": resolved_model,
                    "worker": active_worker_id,
                    "system_fingerprint": f"fp_antigravity_{active_worker_id}",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "logprobs": None,
                            "finish_reason": "stop"
                        }
                    ]
                }
                await sse_response.write(f"data: {json.dumps(finish_chunk)}\n\n".encode("utf-8"))

            elif event_type == "error":
                err_msg = event.get("error", "Stream error")
                err_chunk = {
                    "error": {
                        "message": err_msg,
                        "type": "api_error"
                    }
                }
                await sse_response.write(f"data: {json.dumps(err_chunk)}\n\n".encode("utf-8"))

        # Final SSE termination marker
        await sse_response.write(b"data: [DONE]\n\n")

    except asyncio.CancelledError:
        logger.info("SSE client disconnected from stream: %s", completion_id)
        raise
    except Exception as ex:
        logger.exception("Error during SSE stream: %s", ex)
        err_chunk = {"error": {"message": str(ex), "type": "stream_error"}}
        await sse_response.write(f"data: {json.dumps(err_chunk)}\n\n".encode("utf-8"))
        await sse_response.write(b"data: [DONE]\n\n")
    finally:
        # If ephemeral mode is enabled, purge conversation artifacts immediately
        if should_cleanup and final_conv_id:
            from agy_runner import cleanup_conversation
            cleanup_conversation(final_conv_id, data_dir=active_worker_data_dir)

    return sse_response

async def handle_completions(request: web.Request) -> web.StreamResponse:
    """OpenAI-compatible POST /v1/completions (legacy format adapter)."""
    try:
        body = await request.json()
    except Exception as e:
        return web.json_response({"error": {"message": f"Invalid JSON: {str(e)}"}}, status=400)

    prompt = body.get("prompt", "")
    if isinstance(prompt, list):
        prompt = "\n".join(str(p) for p in prompt)

    # Wrap as a chat completion request
    fake_messages = [{"role": "user", "content": prompt}]
    body["messages"] = fake_messages
    request._read_bytes = json.dumps(body).encode("utf-8")
    return await handle_chat_completions(request)

def create_app() -> web.Application:
    app = web.Application(middlewares=[cors_and_auth_middleware])
    app.router.add_get("/", handle_root)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/v1/health", handle_health)
    app.router.add_get("/v1/models", handle_list_models)
    app.router.add_get("/v1/models/{model_id}", handle_retrieve_model)
    app.router.add_post("/v1/chat/completions", handle_chat_completions)
    app.router.add_post("/v1/completions", handle_completions)
    return app

def main():
    app = create_app()
    print(f"============================================================")
    print(f"🚀 AGY CLI-to-API Bridge Server (OpenClaw compatible)")
    print(f"🔒 Host: {config.HOST} (Strict Localhost Policy)")
    print(f"🌐 Port: {config.PORT}")
    print(f"🤖 Default Model: {config.DEFAULT_MODEL}")
    print(f"============================================================")
    web.run_app(app, host=config.HOST, port=config.PORT)

if __name__ == "__main__":
    main()
