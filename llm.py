"""LLM 백엔드 선택: openai / gemini / offline.

- LLM_PROVIDER 환경변수로 강제할 수 있고, 없으면 .env에 있는 API 키를 보고 고른다.
- 키가 하나도 없으면 offline 모드로 동작한다. offline 모드는 LLM 대신 규칙으로
  초안을 만들고 점수를 매기므로, API 키 없이도 데모 전체 흐름(자동 발행 / 승인 대기 /
  재개)을 확인할 수 있다.
"""
from __future__ import annotations

import json
import os
import re

from dotenv import load_dotenv

load_dotenv()


def resolve_provider() -> str:
    forced = os.getenv("LLM_PROVIDER", "").strip().lower()
    if forced:
        return forced
    if os.getenv("OPENAI_API_KEY"):
        return "openai"
    if os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"):
        return "gemini"
    return "offline"


def provider_label() -> str:
    p = resolve_provider()
    if p == "openai":
        return f"OpenAI · {os.getenv('OPENAI_MODEL', 'gpt-4.1-mini')}"
    if p == "gemini":
        return f"Gemini · {os.getenv('GEMINI_MODEL', 'gemini-3.5-flash')}"
    return "offline (규칙 기반 대체 모드)"


_chat = None


def get_chat():
    """LangChain chat model. offline이면 None."""
    global _chat
    if _chat is not None:
        return _chat
    p = resolve_provider()
    if p == "openai":
        from langchain_openai import ChatOpenAI

        _chat = ChatOpenAI(model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"), temperature=0.3, timeout=90)
    elif p == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI

        _chat = ChatGoogleGenerativeAI(
            model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash"),
            google_api_key=os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"),
            temperature=0.3,
        )
    elif p == "offline":
        return None
    else:
        raise ValueError(f"알 수 없는 LLM_PROVIDER: {p}")
    return _chat


def complete(system: str, user: str) -> str:
    chat = get_chat()
    if chat is None:
        raise RuntimeError("offline 모드에서는 LLM을 호출할 수 없습니다.")
    msg = chat.invoke([("system", system), ("user", user)])
    content = msg.content
    if isinstance(content, list):  # Gemini는 블록 리스트로 줄 때가 있다
        content = "".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in content)
    return content


def parse_json(text: str) -> dict:
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start : end + 1])
