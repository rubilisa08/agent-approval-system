"""그래프를 감싼 작업 API. Streamlit 화면과 CLI가 같이 쓴다.

작업 1건 = LangGraph thread 1개. thread_id를 키로 체크포인터에서 상태를 읽고 이어서 실행한다.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from langgraph.types import Command

from criteria import MAX_REVISIONS, PENDING_SLA_DAYS
from graph import build_graph

_graph = None
_conn = None


def graph():
    global _graph, _conn
    if _graph is None:
        _graph, _conn = build_graph()
    return _graph


def _cfg(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def submit(topic: str, key_points: str, sources: list[str], audience: str = "") -> str:
    """새 발행 요청. 기준에 안 걸리면 이 호출 안에서 발행까지 끝나고, 걸리면 human_review에서 멈춘다."""
    rid = datetime.now().strftime("post-%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    graph().invoke(
        {
            "request_id": rid, "created_at": datetime.now().isoformat(timespec="seconds"),
            "topic": topic.strip(), "key_points": key_points.strip(),
            "sources": [s.strip() for s in sources if s.strip()], "audience": audience.strip(),
            "revision": 0, "history": [],
        },
        _cfg(rid),
    )
    return rid


def resume(thread_id: str, action: str, comment: str = "", edited_text: str = "", reviewer: str = "") -> dict:
    """멈춘 작업에 사람의 답을 주고 멈춘 지점(human_review)부터 이어서 실행한다."""
    job = get(thread_id)
    if not job["_pending"]:
        raise ValueError(f"{thread_id}는 승인 대기 상태가 아닙니다.")
    if action == "revise" and job["_revisions_left"] <= 0:
        raise ValueError(f"재작성 요청은 {MAX_REVISIONS}회까지입니다. 승인·수정 후 승인·반려 중에서 골라 주세요.")
    graph().invoke(
        Command(resume={"action": action, "comment": comment, "edited_text": edited_text, "reviewer": reviewer}),
        _cfg(thread_id),
    )
    return get(thread_id)


def get(thread_id: str) -> dict:
    snap = graph().get_state(_cfg(thread_id))
    values = dict(snap.values)
    values["_pending"] = bool(snap.next) and "human_review" in snap.next
    values["_next"] = list(snap.next)
    interrupts = getattr(snap, "interrupts", ()) or [i for t in snap.tasks for i in t.interrupts]
    values["_interrupt"] = interrupts[0].value if interrupts else None
    values["_revisions_left"] = max(0, MAX_REVISIONS - values.get("revision", 0))
    if values["_pending"]:
        since = values.get("pending_since") or values.get("created_at")
        age = (datetime.now() - datetime.fromisoformat(since)).total_seconds() / 86400 if since else 0.0
        values["_pending_days"] = age
        values["_overdue"] = age >= PENDING_SLA_DAYS
    else:
        values["_pending_days"], values["_overdue"] = None, False
    return values


def format_age(days: float | None) -> str:
    if days is None:
        return "-"
    minutes = int(days * 24 * 60)
    if minutes < 60:
        return f"{minutes}분"
    if minutes < 24 * 60:
        return f"{minutes // 60}시간"
    return f"{days:.1f}일"


def pending_jobs() -> list[dict]:
    """승인 대기 건. 기한(PENDING_SLA_DAYS)을 넘긴 건을 맨 위에, 그 안에서는 오래 기다린 순서로."""
    jobs = [j for j in list_jobs() if j["_pending"]]
    return sorted(jobs, key=lambda j: (not j["_overdue"], -(j["_pending_days"] or 0)))


def is_pending(thread_id: str) -> bool:
    return get(thread_id)["_pending"]


def thread_ids() -> list[str]:
    graph()
    rows = _conn.execute("SELECT DISTINCT thread_id FROM checkpoints").fetchall()
    return [r[0] for r in rows]


def list_jobs() -> list[dict]:
    jobs = [get(t) for t in thread_ids()]
    jobs = [j for j in jobs if j.get("request_id")]
    return sorted(jobs, key=lambda j: j.get("created_at", ""), reverse=True)


def delete_all() -> None:
    graph()
    for t in thread_ids():
        _graph.checkpointer.delete_thread(t)
