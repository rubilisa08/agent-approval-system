"""콘텐츠 자동 발행 파이프라인 (LangGraph + Human-in-the-Loop).

  draft_post → check_facts → evaluate_quality → gate ─┬─(기준 통과)──────────────→ publish → END
                                                        └─(기준 걸림)→ human_review ─┬─ 승인/수정 후 승인 → publish
                                                                    (interrupt로 멈춤) ├─ 반려 → reject → END
                                                                                      └─ 재작성 요청 → draft_post
                                                                                         (MAX_REVISIONS회까지, 넘기면 human_review에서 다시 멈춤)

- 바깥으로 내보내는 단계(publish = 구독자 발송) 바로 앞의 human_review에서 interrupt()로 멈춘다.
- 멈춘 상태는 SqliteSaver 체크포인터(data/checkpoints.sqlite)에 저장된다. 앱을 껐다 켜도
  담당자가 나중에 Command(resume=...)로 답을 주면 멈춘 지점부터 이어서 실행된다.
"""
from __future__ import annotations

import csv
import json
import operator
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

import llm
from criteria import (
    MAX_REVISIONS,
    QUALITY_THRESHOLD,
    check_numeric_sources,
    offline_quality_score,
    review_reasons,
    split_body_and_refs,
)

ROOT = Path(__file__).parent
DATA = ROOT / "data"
OUTBOX = ROOT / "outbox"
DATA.mkdir(exist_ok=True)
OUTBOX.mkdir(exist_ok=True)


class PostState(TypedDict, total=False):
    request_id: str
    created_at: str
    topic: str
    key_points: str
    sources: list[str]
    audience: str
    draft: str
    revision: int
    feedback: str
    fact_findings: list[dict]
    quality: dict
    reasons: list[dict]
    route: str  # "auto" | "human"
    decision: dict
    status: str  # drafting | pending_review | published | rejected
    pending_since: str  # 승인 대기에 들어간 시각 (대기 기한 계산용)
    published: dict
    history: Annotated[list[dict], operator.add]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _log(step: str, message: str) -> list[dict]:
    return [{"at": _now(), "step": step, "message": message}]


# ------------------------------------------------------------------ 1. 초안 작성

DRAFT_SYSTEM = """당신은 뉴스레터 에디터입니다. 요청받은 주제와 핵심 내용으로 구독자에게 보낼 글을 한국어 마크다운으로 씁니다.
규칙:
- 첫 줄은 '# 제목'. 도입 → 본문 소제목(##) 2~4개 → '## 정리' 순서로 구성합니다.
- 수치(%, 명, 원, 배 등)를 쓸 때는 반드시 그 문장 끝에 [번호]로 출처를 표시하고, 마지막에 '## 참고자료' 아래 '[번호] URL' 형식으로 나열합니다.
- 참고자료에는 아래 '제공된 출처'만 쓸 수 있습니다. 출처를 만들어 내지 마세요.
- 대상 독자에 맞는 차분하고 신뢰감 있는 어조로, 과장 표현은 쓰지 않습니다.
- 핵심 내용과 제공된 출처에 없는 사실·평가·제품 정보·전문가 의견을 지어내지 않습니다. 내용이 부족하면 부족한 만큼만 짧게 씁니다.
- 글 본문만 출력합니다."""


def _offline_draft(state: PostState) -> str:
    topic = state["topic"].strip()
    points = [p.strip(" -•\t") for p in state["key_points"].splitlines() if p.strip()]
    sources = [s.strip() for s in state.get("sources", []) if s.strip()]
    feedback = state.get("feedback", "")
    if feedback:  # offline 재작성: 출처 없는 수치 문장을 빼고 과장 표현을 덜어 낸다
        sourced = []
        for p in points:
            if check_numeric_sources(p + ("\n## 참고자료\n" + "\n".join(f"[{i}] {s}" for i, s in enumerate(sources, 1)) if sources else ""), sources):
                continue
            for w in ["대박", "무조건", "역대급", "강추", "꼭 사세요", "지금 당장"]:
                p = p.replace(w, "")
            sourced.append(p.replace("!!!", ".").replace("!!", ".").replace("!", "."))
        points = sourced or ["요청하신 내용을 확인 가능한 사실 위주로 다시 정리했습니다."]
    lines = [f"# {topic}", "", f"{state.get('audience') or '구독자'} 여러분, 이번 호에서는 '{topic}'에 대해 살펴봅니다.", ""]
    for i, p in enumerate(points, 1):
        heading = re.split(r"[.!?](?:\s|$)", p)[0]
        heading = heading if len(heading) <= 28 else heading[:28].rsplit(" ", 1)[0] + "…"
        lines += [f"## {i}. {heading}", "", p if p.endswith((".", "!", "?", "]")) else p + ".", ""]
    lines += ["## 정리", "", f"오늘 살펴본 '{topic}'의 핵심을 정리하면 위 {len(points)}가지입니다. 다음 호에서도 유용한 소식으로 찾아오겠습니다.", ""]
    if sources:
        lines += ["## 참고자료"] + [f"[{i}] {s}" for i, s in enumerate(sources, 1)]
    return "\n".join(lines)


def draft_post(state: PostState) -> dict:
    rev = state.get("revision", 0) + (1 if state.get("feedback") else 0)
    if llm.get_chat() is None:
        draft = _offline_draft(state)
    else:
        srcs = "\n".join(f"[{i}] {s}" for i, s in enumerate(state.get("sources", []), 1)) or "(없음 — 수치를 쓰려면 출처가 없다는 점에 유의)"
        user = (
            f"주제: {state['topic']}\n대상 독자: {state.get('audience') or '일반 구독자'}\n\n"
            f"핵심 내용:\n{state['key_points']}\n\n제공된 출처:\n{srcs}"
        )
        if state.get("feedback"):
            user += f"\n\n[이전 초안]\n{state.get('draft', '')}\n\n[담당자 수정 요청]\n{state['feedback']}\n위 요청을 반영해 다시 쓰세요."
        draft = llm.complete(DRAFT_SYSTEM, user).strip()
    msg = "초안 작성" if rev == 0 else f"담당자 피드백 반영 재작성 (v{rev + 1})"
    return {"draft": draft, "revision": rev, "status": "drafting", "history": _log("draft_post", msg)}


# ------------------------------------------------------------------ 2. 규칙 기반 팩트/출처 검사


def check_facts(state: PostState) -> dict:
    findings = check_numeric_sources(state["draft"], state.get("sources", []))
    msg = "수치 주장 출처 모두 확인됨" if not findings else f"출처 미확인 수치 주장 {len(findings)}건 발견"
    return {"fact_findings": findings, "history": _log("check_facts", msg)}


# ------------------------------------------------------------------ 3. LLM 품질 평가 (Eval)

EVAL_SYSTEM = """당신은 깐깐한 뉴스레터 품질 검수자입니다. 요청(주제·핵심 내용·제공된 출처)과 생성된 글을 비교해 채점합니다.
먼저 근거 없는 주장을 찾습니다:
- unsupported_claims: 핵심 내용이나 제공된 출처에 없는 내용을 단정한 문장. 각 문장에 type을 하나 붙입니다.
  · "evidence": 외부 근거를 내세움 — 조사·연구·전문가·테스트·사용자 평가·리뷰·판매 실적·인기
    (예: "음향 전문가들의 테스트를 거쳐", "사용자들의 긍정적인 평가를 받고 있어", "최근 연구에 따르면")
  · "specific": 특정 제품·기업·인물·사건이나 제품 성능을 구체적으로 단정 (예: "배터리 지속 시간도 만족스러운 수준")
  · "general": 핵심 내용을 풀어 쓴 설명, 상식 수준의 이점 설명 (예: "집중력 유지에 도움을 줍니다")
채점 (감점 사유를 issues에 적었다면 반드시 해당 항목 점수에 반영합니다. 사유를 적고 만점에 가깝게 주지 마세요):
- tone (0~30): 대상 독자에게 맞는 차분하고 신뢰감 있는 어조인가. 과장·선동·구어체가 있으면 감점.
- context (0~40): 핵심 내용을 빠짐없이, 왜곡 없이 반영했는가. evidence·specific 주장 1건당 10점 이상 감점(general은 감점하지 않음).
- completeness (0~30): 구독자가 읽고 얻는 정보가 충분한가. 구체적 정보 없이 일반론만 있으면 15점 이하.
JSON 한 개만 출력합니다:
{"unsupported_claims": [{"text": "문장", "type": "evidence|specific|general"}, ...], "tone": 정수, "context": 정수, "completeness": 정수, "issues": ["감점 사유", ...], "rationale": "한두 문장 총평"}"""


def _claim(c) -> dict:
    if isinstance(c, dict):
        t = str(c.get("type", "")).lower()
        return {"text": str(c.get("text", "")).strip(), "type": t if t in ("evidence", "specific", "general") else "specific"}
    return {"text": str(c).strip(), "type": "specific"}  # 형식이 어긋나면 보수적으로 기준에 넣는다


def evaluate_quality(state: PostState) -> dict:
    if llm.get_chat() is None:
        q = offline_quality_score(state["topic"], state["draft"])
    else:
        try:
            raw = llm.complete(
                EVAL_SYSTEM,
                f"요청 주제: {state['topic']}\n핵심 내용:\n{state['key_points']}\n"
                f"제공된 출처: {', '.join(state.get('sources', [])) or '(없음)'}\n"
                + (f"담당자 수정 요청: {state['feedback']}\n(이 요청에 따라 빠진 핵심 내용은 감점하지 않습니다.)\n" if state.get("feedback") else "")
                + f"\n[생성된 글]\n{state['draft']}",
            )
            j = llm.parse_json(raw)
            tone = max(0, min(30, int(j["tone"])))
            ctx = max(0, min(40, int(j["context"])))
            comp = max(0, min(30, int(j["completeness"])))
            q = {
                "score": tone + ctx + comp, "tone": tone, "context": ctx, "completeness": comp,
                "issues": list(j.get("issues", [])), "rationale": str(j.get("rationale", "")),
                "claims_all": [_claim(c) for c in j.get("unsupported_claims", [])],
                "evaluator": llm.provider_label(),
            }
            # 승인 기준에는 외부 근거 사칭(evidence)과 구체적 사실 단정(specific)만 쓴다. general은 참고로만 보여 준다.
            q["unsupported_claims"] = [c["text"] for c in q["claims_all"] if c["type"] in ("evidence", "specific") and c["text"]]
        except Exception as e:  # 평가 실패는 '통과'가 아니라 '사람 확인'으로 보낸다
            q = {"score": None, "error": f"{type(e).__name__}: {e}"[:200], "issues": [], "rationale": "", "evaluator": llm.provider_label()}
    msg = f"품질 평가 오류 — {q['error']}" if q.get("error") else f"검수 점수 {q['score']}점 (기준 {QUALITY_THRESHOLD}점)"
    return {"quality": q, "history": _log("evaluate_quality", msg)}


# ------------------------------------------------------------------ 4. 게이트: 사람 승인 필요 여부


def gate(state: PostState) -> dict:
    reasons = review_reasons(state.get("fact_findings", []), state.get("quality", {}), state.get("revision", 0))
    if reasons:
        return {"reasons": reasons, "route": "human", "status": "pending_review", "pending_since": _now(),
                "history": _log("gate", "승인 대기로 보냄: " + ", ".join(r["label"] for r in reasons))}
    return {"reasons": [], "route": "auto", "history": _log("gate", "모든 기준 통과 → 사람 확인 없이 자동 발행")}


def route_after_gate(state: PostState) -> Literal["human_review", "publish"]:
    return "human_review" if state.get("route") == "human" else "publish"


# ------------------------------------------------------------------ 5. 사람 승인 (HITL)


def human_review(state: PostState) -> Command[Literal["publish", "reject", "draft_post", "human_review"]]:
    # 재개 시 이 노드는 처음부터 다시 실행되므로, interrupt() 앞에는 부작용 있는 코드를 두지 않는다.
    revisions_left = max(0, MAX_REVISIONS - state.get("revision", 0))
    answer = interrupt({
        "request_id": state["request_id"],
        "reasons": state.get("reasons", []),
        "draft": state["draft"],
        "on_approve": f"구독자 {subscriber_count()}명에게 이메일 발송",
        "revisions_left": revisions_left,
    })
    action = answer.get("action")
    decision = {**answer, "at": _now()}
    reviewer = answer.get("reviewer") or "담당자"
    if action == "revise" and revisions_left <= 0:
        # 상한을 넘긴 재작성 요청은 받지 않고 같은 자리에서 다시 멈춘다 (발송·반려 어느 쪽으로도 가지 않음)
        return Command(goto="human_review", update={
            "history": _log("human_review", f"재작성 요청 거절: 상한 {MAX_REVISIONS}회에 도달 — 승인·수정 후 승인·반려 중 선택 필요"),
        })
    if action == "approve":
        update = {"decision": decision, "history": _log("human_review", f"{reviewer} 승인")}
        if answer.get("edited_text", "").strip() and answer["edited_text"].strip() != state["draft"].strip():
            edited = answer["edited_text"].strip()
            left = check_numeric_sources(edited, state.get("sources", []))
            note = f" (수정본에 출처 미확인 수치 {len(left)}건 남음 — 담당자 판단으로 발송)" if left else ""
            update["draft"] = edited
            update["fact_findings"] = left
            update["history"] = _log("human_review", f"{reviewer}가 본문을 직접 수정한 뒤 승인{note}")
        return Command(goto="publish", update=update)
    if action == "revise":
        return Command(goto="draft_post", update={
            "decision": decision, "feedback": answer.get("comment", ""),
            "history": _log("human_review", f"{reviewer} 재작성 요청: {answer.get('comment', '')}"),
        })
    return Command(goto="reject", update={
        "decision": decision, "history": _log("human_review", f"{reviewer} 반려: {answer.get('comment', '')}"),
    })


# ------------------------------------------------------------------ 6. 발행 / 반려


def load_subscribers() -> list[str]:
    path = DATA / "subscribers.csv"
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return [r["email"] for r in csv.DictReader(f) if r.get("email")]


def subscriber_count() -> int:
    return len(load_subscribers())


def publish(state: PostState) -> dict:
    """외부로 내보내는 단계. 데모에서는 실제 메일 대신 outbox/에 발송본과 발송 로그를 남긴다."""
    recipients = load_subscribers()
    path = OUTBOX / f"{state['request_id']}.md"
    path.write_text(state["draft"], encoding="utf-8")
    mode = "자동 발행" if state.get("route") == "auto" else "승인 후 발행"
    record = {
        "request_id": state["request_id"], "at": _now(), "mode": mode, "topic": state["topic"],
        "recipients": len(recipients), "file": str(path.relative_to(ROOT)),
    }
    with (OUTBOX / "send_log.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return {"status": "published", "published": record,
            "history": _log("publish", f"{mode}: 구독자 {len(recipients)}명에게 발송 (outbox/{path.name})")}


def reject(state: PostState) -> dict:
    return {"status": "rejected", "history": _log("reject", "발송하지 않고 종료")}


# ------------------------------------------------------------------ 그래프 조립


def build_graph(db_path: Path | str | None = None):
    db_path = db_path or os.getenv("CHECKPOINT_DB") or DATA / "checkpoints.sqlite"
    g = StateGraph(PostState)
    g.add_node("draft_post", draft_post)
    g.add_node("check_facts", check_facts)
    g.add_node("evaluate_quality", evaluate_quality)
    g.add_node("gate", gate)
    g.add_node("human_review", human_review)
    g.add_node("publish", publish)
    g.add_node("reject", reject)

    g.add_edge(START, "draft_post")
    g.add_edge("draft_post", "check_facts")
    g.add_edge("check_facts", "evaluate_quality")
    g.add_edge("evaluate_quality", "gate")
    g.add_conditional_edges("gate", route_after_gate, ["human_review", "publish"])
    g.add_edge("publish", END)
    g.add_edge("reject", END)

    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    return g.compile(checkpointer=SqliteSaver(conn)), conn


__all__ = ["build_graph", "PostState", "split_body_and_refs", "subscriber_count"]
