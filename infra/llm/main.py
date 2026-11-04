"""Deterministic-first router in front of LiteLLM.

Rules applied in order, first match wins (section 5 of the hardware contract):
  1. < ~2K tokens, no attachment, <= 3 turns        -> family (P1000, 4B)
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
API_KEY = os.environ.get("LITELLM_KEY", "")
CLOUD_MODEL = os.environ.get("CLOUD_MODEL", "cloud")
CLOUD_ENABLED = os.environ.get("CLOUD_ENABLED", "false").lower() == "true"

FAMILY_TOKENS = int(os.environ.get("FAMILY_MAX_TOKENS", "2000"))
DEEP_TOKENS = int(os.environ.get("DEEP_MIN_TOKENS", "12000"))
CLOUD_TOKENS = int(os.environ.get("CLOUD_MIN_TOKENS", "32000"))

PLANNING = re.compile(
    r"\b(plan|architect|architecture|migrate|migration|refactor|redesign|roadmap)\b"
    r"|\bacross (?:files|modules|services)\b",
    re.I,
)

app = FastAPI()


def est_tokens(messages):
    """Cheap token estimate: ~4 chars/token plus per-message overhead."""
    chars = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            chars += len(c)
        elif isinstance(c, list):
            for part in c:
                if isinstance(part, dict) and part.get("type") == "text":
                    chars += len(part.get("text", ""))
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
        "FAMILY = everyday question, short answer is enough.\n"
        "CODER = code, debugging, tool use, or deep reasoning. "
        f"Turns so far: {turns}."
    )
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(
            f"{LITELLM}/chat/completions",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={
                "model": "family",
                "messages": [{"role": "system", "content": prompt}] + messages[-4:],
                "max_tokens": 60,
                "temperature": 0,
                "chat_template_kwargs": {"enable_thinking": False},
            },
        )
        r.raise_for_status()
        out = r.json()["choices"][0]["message"].get("content", "")
    return "coder" if "CODER" in out.upper() else "family"


def decide(messages):
    """Return (model, rule, reason). Deterministic rules first."""
    tokens = est_tokens(messages)
    turns = sum(1 for m in messages if m.get("role") == "user")
    attached = has_attachment(messages)
    text = " ".join(m.get("content") for m in messages if isinstance(m.get("content"), str))

    if attached:
        # uploads stay local, always
        return ("coder", "rule2-attachment", f"{tokens} tok with attachment")
    if tokens >= CLOUD_TOKENS or explicit_cloud(messages):
        return ("cloud", "rule4-cloud", f"{tokens} tok or explicit request")
    if tokens >= DEEP_TOKENS or PLANNING.search(text):
        return ("coder", "rule3-deep", f"{tokens} tok or planning intent")
    if tokens >= FAMILY_TOKENS or turns >= 4:
        return ("coder", "rule2-size", f"{tokens} tok / {turns} turns")
    if turns <= 3:
        return ("family", "rule1-short", f"{tokens} tok / {turns} turns")
    return (None, "rule5-judge", f"{tokens} tok / {turns} turns")


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/v1/models")
async def models():
    """Passthrough so a UI pointed at the router still lists models."""
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{LITELLM}/models", headers={"Authorization": f"Bearer {API_KEY}"})
    return JSONResponse(status_code=r.status_code, content=r.json())


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    messages = body.get("messages", [])

    # No local vision model is deployed, and forwarding a picture to the text-only
    # coder returns an opaque upstream 500. Refuse it up front with a usable message.
    if attachment_kind(messages) == "image":
        return JSONResponse(
            status_code=400,
            content={"error": "image input is not supported: no vision model (mmproj) is deployed "
                              "for the local models. Send the image as extracted text, or route "
                              "explicitly to a cloud model.",
                     "_router": {"model": None, "rule": "precheck-vision", "reason": "image attachment"}},
        )

    model, rule, reason = decide(messages)

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

    body.pop("chat_template_kwargs", None) if model == "family" else None
    if model == "family":
        body.setdefault("chat_template_kwargs", {"enable_thinking": False})

    headers = {"Content-Type": "application/json"}
    if upstream_key:
        headers["Authorization"] = f"Bearer {upstream_key}"
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
