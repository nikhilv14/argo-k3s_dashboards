"""Deterministic-first router in front of LiteLLM.

Rules applied in order, first match wins (section 5 of the hardware contract):
  1. < ~2K tokens, no attachment, <= 3 turns        -> coder (only local tier)
  2. >= 2K tokens, or a document/page upload,
     or >= 4 turns                                  -> coder (5060 Ti, 27B)
  3. >= 12K tokens, or planning keywords            -> coder, deep settings
  4. >= 32K tokens, or explicit user request        -> cloud (OpenRouter)
  5. none of the above                              -> cheap judge, 60-token rubric

Anything carrying an uploaded document never goes to cloud.
"""
import json
import os
import re

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

LITELLM = os.environ.get("LITELLM_BASE", "http://litellm.llm.svc.cluster.local:4000/v1")
# embeddings: P1000 GPU first, CPU pod if the card/VM stops answering
CODER_URL = os.environ.get("CODER_URL", "http://192.168.12.243:8081")
# auto mode: local coder only while it is idle and the conversation is short enough
# to leave room for the reply (n_ctx is 65536, so 54000 leaves ~11k for output)
AUTO_MAX_TOKENS = int(os.environ.get("AUTO_MAX_TOKENS", "54000"))
EMBED_PRIMARY = os.environ.get("EMBED_PRIMARY", "http://192.168.12.245:8090")
EMBED_FALLBACK = os.environ.get("EMBED_FALLBACK", "http://llm-embed-cpu.llm.svc.cluster.local:8082")
API_KEY = os.environ.get("LITELLM_KEY", "")
CLOUD_MODEL = os.environ.get("CLOUD_MODEL", "cloud")
# Which model ids this router advertises at /v1/models. Empty = advertise
# everything upstream offers (plus "auto"). Set to a subset to keep a public
# UI (chat.daemonagent.net) on a single choice without touching LiteLLM, so
# other clients pointed at litellm directly keep the full catalogue.
EXPOSED_MODELS = [m.strip() for m in os.environ.get("EXPOSED_MODELS", "").split(",") if m.strip()]
# LiteLLM alias for the local 27B on the 5060 Ti
LOCAL = os.environ.get("LOCAL_MODEL", "qwen3.8-27b")
CLOUD_ENABLED = os.environ.get("CLOUD_ENABLED", "false").lower() == "true"

DEEP_TOKENS = int(os.environ.get("DEEP_MIN_TOKENS", "12000"))
CLOUD_TOKENS = int(os.environ.get("CLOUD_MIN_TOKENS", "32000"))

PLANNING = re.compile(
    r"\b(plan|architect|architecture|migrate|migration|refactor|redesign|roadmap)\b"
    r"|\bacross (?:files|modules|services)\b",
    re.I,
)

app = FastAPI()


def est_tokens(messages, tools=None):
    """Cheap token estimate: ~4 chars/token plus per-message overhead.

    Tool schemas must be counted: an OpenAI-compatible UI (Open WebUI) sends
    several thousand tokens of tool definitions on every request, which is
    enough to overflow a small model's context on its own.
    """
    chars = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            chars += len(c)
        elif isinstance(c, list):
            for part in c:
                if isinstance(part, dict) and part.get("type") == "text":
                    chars += len(part.get("text", ""))
    if tools:
        chars += len(json.dumps(tools))
    return chars // 4 + 4 * len(messages)


def attachment_kind(messages):
    """Return 'image' if any message carries a picture, 'file' for other uploads, else None."""
    kind = None
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            for part in c:
                if not isinstance(part, dict):
                    continue
                if part.get("type") in ("image_url", "input_image"):
                    return "image"
                if part.get("type") in ("file", "input_file"):
                    kind = "file"
    return kind


def has_attachment(messages):
    return attachment_kind(messages) is not None


def explicit_cloud(messages):
    text = " ".join(
        m.get("content") for m in messages
        if m.get("role") == "user" and isinstance(m.get("content"), str)
    ).lower()
    return "@cloud" in text or "use the cloud" in text or "ask openrouter" in text


async def judge(messages, turns):
    """60-token rubric, single label out. Only reached when no rule matched."""
    prompt = (
        "Classify the user's request. Answer with exactly one word.\n"
        "CODER = code, debugging, tool use, or deep reasoning. "
        f"Turns so far: {turns}."
    )
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(
            f"{LITELLM}/chat/completions",
            headers=headers,
            json={
                "model": LOCAL,
                "messages": [{"role": "system", "content": prompt}] + messages[-4:],
                "max_tokens": 60,
                "temperature": 0,
                "chat_template_kwargs": {"enable_thinking": False},
            },
        )
        r.raise_for_status()
        out = r.json()["choices"][0]["message"].get("content", "")
    return LOCAL  # single local tier; kept for the ambiguous-shape path


async def coder_state():
    """'idle' | 'busy' | 'down' — is the local 27B free right now?"""
    try:
        async with httpx.AsyncClient(timeout=2.5) as c:
            r = await c.get(f"{CODER_URL}/slots")
        slots = r.json()
        if any(s.get("is_processing") for s in slots if isinstance(s, dict)):
            return "busy"
        return "idle"
    except Exception:
        return "down"


def decide(messages, tools=None, requested=None, coder=None):
    """Return (model, rule, reason).

    An explicit model choice wins: if the caller asked for "cloud" or "coder"
    (Open WebUI's model picker), honour it instead of re-deciding. The
    deterministic rules only apply to requests that do not name a model.
    """
    if requested in ("auto", "Auto"):
        tokens = est_tokens(messages, tools)
        if tokens > AUTO_MAX_TOKENS:
            return ("cloud", "auto-long", f"{tokens} tok > {AUTO_MAX_TOKENS} limit")
        if coder == "busy":
            return ("cloud", "auto-busy", "local coder busy (hermes/opencode or another chat)")
        if coder == "down":
            return ("cloud", "auto-down", "local coder unreachable")
        return (LOCAL, "auto-local", f"{tokens} tok, coder idle")
    if requested in ("cloud", "cloud-nous"):
        return ("cloud", "explicit-cloud", "client selected cloud")
    if requested in ("coder", "coder-27b", LOCAL):
        return (LOCAL, "explicit-coder", "client selected the local model")
    tokens = est_tokens(messages, tools)
    turns = sum(1 for m in messages if m.get("role") == "user")
    attached = has_attachment(messages)
    text = " ".join(m.get("content") for m in messages if isinstance(m.get("content"), str))

    if attached:
        # uploads stay local, always
        return (LOCAL, "rule2-attachment", f"{tokens} tok with attachment")
    if tokens >= CLOUD_TOKENS or explicit_cloud(messages):
        return ("cloud", "rule4-cloud", f"{tokens} tok or explicit request")
    if tokens >= DEEP_TOKENS or PLANNING.search(text):
        return (LOCAL, "rule3-deep", f"{tokens} tok or planning intent")
    # No second local tier any more: the P1000 4B was retired (the 27B on the 5060 Ti
    # is both faster and stronger), so everything local lands on the coder.
    return (LOCAL, "rule1-default", f"{tokens} tok / {turns} turns")


async def stream_upstream(body, headers, model, rule, reason):
    """Relay SSE verbatim. Errors are surfaced as JSON before the stream opens."""
    client = httpx.AsyncClient(timeout=None)
    req = client.build_request("POST", f"{LITELLM}/chat/completions", headers=headers, json=body)
    try:
        resp = await client.send(req, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        return JSONResponse(status_code=502, content={"error": f"upstream transport error: {exc}",
                                                      "_router": {"model": model, "rule": rule}})
    if resp.status_code != 200:
        raw = await resp.aread()
        await resp.aclose()
        await client.aclose()
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = raw[:400].decode("utf-8", "replace")
        return JSONResponse(status_code=resp.status_code,
                            content={"upstream": payload,
                                     "_router": {"model": model, "rule": rule, "reason": reason}})

    async def relay():
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(relay(), media_type="text/event-stream",
                             headers={"X-Router-Model": model, "X-Router-Rule": rule})


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/v1/models")
async def models():
    """Passthrough so a UI pointed at the router still lists models.

    When EXPOSED_MODELS is set, only those ids are advertised — the public
    chat UI then offers exactly that list. Requests naming a hidden model are
    still routed by decide(), so nothing breaks for clients that talk to
    LiteLLM directly.
    """
    headers = {}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{LITELLM}/models", headers=headers)
    try:
        data = r.json()
        rows = [m for m in data.get("data", []) if isinstance(m, dict)]
        if EXPOSED_MODELS:
            rows = [m for m in rows if m.get("id") in EXPOSED_MODELS]
        data["data"] = rows
        ids = {m.get("id") for m in rows}
        if "auto" not in ids and (not EXPOSED_MODELS or "auto" in EXPOSED_MODELS):
            data["data"].append({
                "id": "auto", "object": "model", "created": 0,
                "owned_by": "router",
            })
        return JSONResponse(status_code=r.status_code, content=data)
    except ValueError:
        return JSONResponse(status_code=502, content={"error": "upstream returned non-JSON",
                                                      "upstream_status": r.status_code})


@app.post("/v1/embeddings")
async def embeddings(request: Request):
    """Proxy embeddings with failover: P1000 GPU -> CPU pod."""
    raw = await request.body()
    headers = {"Content-Type": "application/json"}
    for name, base in (("gpu", EMBED_PRIMARY), ("cpu", EMBED_FALLBACK)):
        try:
            async with httpx.AsyncClient(timeout=120) as c:
                r = await c.post(f"{base}/v1/embeddings", content=raw, headers=headers)
            if r.status_code == 200:
                if name == "cpu":
                    print("EMBED fallback used (gpu unavailable)", flush=True)
                return JSONResponse(r.json(), headers={"X-Embed-Backend": name})
            print(f"EMBED {name} -> HTTP {r.status_code}", flush=True)
        except Exception as e:
            print(f"EMBED {name} -> {type(e).__name__}: {str(e)[:80]}", flush=True)
    return JSONResponse({"error": "all embedding backends failed"}, status_code=503)


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    messages = body.get("messages", [])

    # The coder runs with an mmproj projector (llama-coder.service on the GPU VM), so
    # pictures are routed locally. If that projector is ever removed, this path will
    # surface the upstream error verbatim rather than silently guessing.
    requested = body.get("model")
    coder = await coder_state() if requested in ("auto", "Auto") else None
    model, rule, reason = decide(messages, body.get("tools"), requested, coder)
    # one line per decision: tells us which model the client asked for and what we did
    print(f"ROUTE requested={requested!r} -> {model} rule={rule} ({reason}) "
          f"stream={bool(body.get('stream'))} tools={len(body.get('tools') or [])}",
          flush=True)

    if model is None:
        model = await judge(messages, sum(1 for m in messages if m.get("role") == "user"))

    if model == "cloud":
        if not CLOUD_ENABLED:
            # Never silently fall back to a broken path: be explicit.
            return JSONResponse(
                status_code=503,
                content={"error": "cloud route selected but no provider is enabled (CLOUD_ENABLED=false)",
                         "rule": rule, "reason": reason},
            )
        body["model"] = CLOUD_MODEL
        upstream_key = API_KEY
    else:
        body["model"] = model
        upstream_key = API_KEY

    headers = {"Content-Type": "application/json"}
    if upstream_key:
        headers["Authorization"] = f"Bearer {upstream_key}"
    if body.get("stream"):
        return await stream_upstream(body, headers, model, rule, reason)

    try:
        async with httpx.AsyncClient(timeout=600) as c:
            r = await c.post(f"{LITELLM}/chat/completions", headers=headers, json=body)
    except httpx.HTTPError as exc:
        return JSONResponse(
            status_code=502,
            content={"error": f"upstream transport error: {type(exc).__name__}: {exc}",
                     "_router": {"model": model, "rule": rule, "reason": reason}},
        )

    try:
        payload = r.json()
    except ValueError:
        return JSONResponse(
            status_code=502,
            content={"error": "upstream returned non-JSON",
                     "upstream_status": r.status_code, "upstream_body": r.text[:400],
                     "_router": {"model": model, "rule": rule, "reason": reason}},
        )

    # Surface upstream failures with the routing decision, never as a bare 500.
    if r.status_code != 200:
        return JSONResponse(
            status_code=r.status_code,
            content={"upstream": payload,
                     "_router": {"model": model, "rule": rule, "reason": reason}},
        )

    payload["_router"] = {"model": model, "rule": rule, "reason": reason}
    return JSONResponse(status_code=200, content=payload)
