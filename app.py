"""콘텐츠 자동 발행 — 승인 대기함 (Streamlit 데모).

실행: streamlit run app.py
"""
from __future__ import annotations

import html
import re

import streamlit as st

import llm
import service
from criteria import MAX_REVISIONS, PENDING_SLA_DAYS, QUALITY_THRESHOLD
from graph import subscriber_count
from samples import SAMPLES

st.set_page_config(page_title="콘텐츠 발행 승인함", page_icon="📨", layout="wide")

st.markdown(
    """
<style>
.chip{display:inline-block;padding:2px 10px;margin:0 6px 4px 0;border-radius:999px;font-size:0.85rem;font-weight:600}
.chip-red{background:#fde2e1;color:#b42318}
.chip-amber{background:#fef0c7;color:#b54708}
.chip-blue{background:#e0eaff;color:#3538cd}
.chip-green{background:#dcfae6;color:#067647}
.chip-gray{background:#eaecf0;color:#344054}
.chip-purple{background:#ebe9fe;color:#5925dc}
mark{background:#fde2e1;color:inherit;padding:0 2px;border-bottom:2px solid #f04438}
mark.claim{background:#ebe9fe;border-bottom-color:#7a5af8}
.step{font-size:0.9rem;margin:2px 0}
.step code{font-size:0.8rem}
</style>
""",
    unsafe_allow_html=True,
)

REASON_CHIP = {"NUMERIC_NO_SOURCE": "chip-red", "UNSUPPORTED_CLAIM": "chip-purple", "LOW_QUALITY": "chip-amber", "EVAL_ERROR": "chip-gray", "REVISION_CONFIRM": "chip-blue"}
STATUS_LABEL = {"pending_review": "⏸️ 승인 대기", "published": "📨 발송됨", "rejected": "🚫 반려", "drafting": "✍️ 작성 중"}


def chips(reasons: list[dict]) -> str:
    return "".join(f'<span class="chip {REASON_CHIP.get(r["code"], "chip-gray")}">{html.escape(r["label"])}</span>' for r in reasons)


def highlight(post: str, findings: list[dict], claims: list[str] = ()) -> str:
    """출처 없는 수치 문장(빨강)과 근거 없는 주장(보라)을 <mark>로 표시한 마크다운."""
    out = html.escape(post, quote=False)
    out = re.sub(r"^(#{1,3})\s", lambda m: "#" * (len(m.group(1)) + 2) + " ", out, flags=re.M)  # 미리보기용으로 제목 크기를 줄인다
    for f in findings:
        s = html.escape(f["sentence"], quote=False)
        out = out.replace(s, f"<mark>{s}</mark>", 1)
    for c in claims:  # LLM이 문장을 조금 바꿔 인용할 수 있어, 본문에 그대로 있을 때만 표시한다
        s = html.escape(c.strip().strip('"“”'), quote=False)
        if s and s in out:
            out = out.replace(s, f'<mark class="claim">{s}</mark>', 1)
    return out


def short_reason(job: dict) -> str:
    parts = []
    for r in job.get("reasons", []):
        if r["code"] == "LOW_QUALITY":
            parts.append(f"검수 점수 {job['quality']['score']}점")
        elif r["code"] == "NUMERIC_NO_SOURCE":
            parts.append(f"숫자 출처 누락 {len(job.get('fact_findings', []))}건")
        elif r["code"] == "UNSUPPORTED_CLAIM":
            parts.append(f"근거 없는 주장 {len(job['quality'].get('unsupported_claims', []))}건")
        else:
            parts.append(r["label"])
    return " · ".join(parts)


# ------------------------------------------------------------------ 사이드바

with st.sidebar:
    st.header("📨 콘텐츠 발행 승인함")
    st.caption(f"LLM: **{llm.provider_label()}**")
    st.caption(f"구독자: **{subscriber_count()}명** (data/subscribers.csv)")
    st.caption(f"체크포인터: `data/checkpoints.sqlite`")
    st.divider()
    st.markdown("**사람 승인이 필요한 경우**")
    st.markdown(
        f"""
- <span class="chip chip-red">숫자 출처 누락</span> 수치 주장에 확인 가능한 출처가 없음
- <span class="chip chip-purple">근거 없는 주장</span> 요청 내용·출처에 없는 사실을 LLM 검수가 발견
- <span class="chip chip-amber">검수 점수 미달</span> LLM 검수 점수 {QUALITY_THRESHOLD}점 미만
- <span class="chip chip-gray">품질 평가 실패</span> 평가 노드 오류
- <span class="chip chip-blue">재작성본 확인</span> 담당자가 재작성을 요청한 글

그 외에는 **사람을 거치지 않고 자동 발송**됩니다.

**운영 규칙**
- 재작성 요청은 글 하나당 **{MAX_REVISIONS}회**까지
- **{PENDING_SLA_DAYS:g}일** 넘게 대기한 글은 목록 맨 위에 빨간색으로 표시 (자동 발송·자동 반려 없음)
""",
        unsafe_allow_html=True,
    )
    st.divider()
    if st.button(f"샘플 요청 {len(SAMPLES)}건 한 번에 넣기", use_container_width=True):
        with st.spinner("에이전트가 샘플을 처리하는 중..."):
            for s in SAMPLES.values():
                service.submit(s["topic"], s["key_points"], s["sources"].splitlines(), s["audience"])
        st.rerun()
    with st.expander("데이터 초기화"):
        st.caption("체크포인터의 모든 작업을 지웁니다. outbox/ 발송 기록은 남습니다.")
        if st.button("모든 작업 삭제", type="secondary"):
            service.delete_all()
            st.rerun()

jobs = service.list_jobs()
pending = sorted([j for j in jobs if j["_pending"]], key=lambda j: (not j["_overdue"], -(j["_pending_days"] or 0)))

tab_inbox, tab_new, tab_log = st.tabs([f"⏸️ 승인 대기함 ({len(pending)})", "✍️ 새 발행 요청", "📜 처리 이력"])

# ------------------------------------------------------------------ 새 발행 요청

with tab_new:
    st.subheader("새 발행 요청")
    st.caption("에이전트가 초안 작성 → 출처 검사 → 품질 평가를 거쳐, 기준에 걸리지 않으면 바로 발송하고 걸리면 승인 대기함에서 멈춥니다.")
    preset = st.selectbox("예시 불러오기", ["(직접 입력)"] + list(SAMPLES))
    s = SAMPLES.get(preset, {"topic": "", "audience": "", "key_points": "", "sources": ""})
    with st.form("new_request"):
        topic = st.text_input("주제", s["topic"])
        audience = st.text_input("대상 독자", s["audience"])
        key_points = st.text_area("핵심 내용 (한 줄에 하나, 수치 근거는 [번호]로 출처와 연결)", s["key_points"], height=160)
        sources = st.text_area("출처 URL (한 줄에 하나, 위에서부터 [1], [2] ...)", s["sources"], height=80)
        submitted = st.form_submit_button("에이전트 실행", type="primary")
    if submitted:
        if not topic.strip() or not key_points.strip():
            st.error("주제와 핵심 내용을 입력해 주세요.")
        else:
            with st.spinner("초안 작성 · 출처 검사 · 품질 평가 중..."):
                rid = service.submit(topic, key_points, sources.splitlines(), audience)
            job = service.get(rid)
            if job["_pending"]:
                st.warning(f"⏸️ `{rid}` 승인 대기함으로 이동했습니다 — {short_reason(job)}")
            else:
                st.success(f"📨 `{rid}` 모든 기준을 통과해 사람 확인 없이 구독자 {job['published']['recipients']}명에게 자동 발송했습니다 (검수 점수 {job['quality']['score']}점).")
            st.button("화면 새로고침")

# ------------------------------------------------------------------ 승인 대기함

with tab_inbox:
    if not pending:
        st.info("승인을 기다리는 글이 없습니다. 왼쪽의 **샘플 요청 한 번에 넣기**나 **새 발행 요청** 탭에서 글을 만들어 보세요.")
    else:
        left, right = st.columns([1, 2.6], gap="large")
        with left:
            st.markdown("#### 대기 목록")
            overdue = [j for j in pending if j["_overdue"]]
            if overdue:
                st.error(f"{PENDING_SLA_DAYS:g}일 넘게 대기 중인 글 {len(overdue)}건 — 먼저 처리해 주세요.", icon="⏰")
            st.caption(f"오래 기다린 순서 · {PENDING_SLA_DAYS:g}일 초과 건이 맨 위")
            options = {j["request_id"]: j for j in pending}
            keys = list(options)
            prev = st.session_state.get("selected")
            sel = st.radio(
                "대기 중인 글",
                keys,
                index=keys.index(prev) if prev in keys else 0,
                format_func=lambda rid: (
                    f"{'⏰ ' if options[rid]['_overdue'] else ''}{options[rid]['topic']}  \n"
                    f"{short_reason(options[rid])} · 대기 {service.format_age(options[rid]['_pending_days'])}"
                ),
                label_visibility="collapsed",
            )
            st.session_state["selected"] = sel
        job = options[sel]
        q = job.get("quality", {})
        findings = job.get("fact_findings", [])
        with right:
            st.markdown(f"### {job['topic']}")
            st.markdown(
                chips(job.get("reasons", []))
                + f'<span class="chip chip-gray">{job["request_id"]}</span>'
                + f'<span class="chip chip-gray">v{job.get("revision", 0) + 1} 초안</span>'
                + f'<span class="chip {"chip-red" if job["_overdue"] else "chip-gray"}">'
                f'{"⏰ " if job["_overdue"] else ""}대기 {service.format_age(job["_pending_days"])}</span>',
                unsafe_allow_html=True,
            )
            if job["_overdue"]:
                st.error(f"이 글은 {PENDING_SLA_DAYS:g}일 넘게 결정을 기다리고 있습니다. 오래 기다렸다는 이유로 자동 발송하지 않으니, 승인하거나 반려해 대기함을 비워 주세요.", icon="⏰")

            # --- 멈춘 이유
            st.markdown("##### ⛔ 멈춘 이유")
            for r in job.get("reasons", []):
                st.error(f"**{r['label']}** — {r['detail']}", icon="⏸️")

            # --- 원문 vs 생성 글
            c1, c2 = st.columns(2, gap="medium")
            with c1:
                st.markdown("##### 📝 요청 원문")
                with st.container(border=True):
                    st.markdown(f"**주제** {job['topic']}")
                    st.markdown(f"**대상 독자** {job.get('audience') or '-'}")
                    st.markdown("**핵심 내용**")
                    st.text(job["key_points"])
                    st.markdown("**제공된 출처**")
                    if job.get("sources"):
                        for i, src in enumerate(job["sources"], 1):
                            st.markdown(f"[{i}] {src}")
                    else:
                        st.caption("(없음)")
                    if job.get("feedback"):
                        st.markdown(f"**직전 재작성 요청** {job['feedback']}")
            with c2:
                claims = q.get("unsupported_claims", [])
                legend = [t for t, on in (("빨강 = 출처 없는 수치", findings), ("보라 = 근거 없는 주장", claims)) if on]
                st.markdown("##### 🤖 생성된 포스팅" + (f"  ({', '.join(legend)})" if legend else ""))
                with st.container(border=True, height=520):
                    st.markdown(highlight(job["draft"], findings, claims), unsafe_allow_html=True)

            # --- 에이전트 판단과 근거
            st.markdown("##### 🔍 에이전트의 판단과 근거")
            j1, j2 = st.columns(2, gap="medium")
            with j1:
                with st.container(border=True):
                    st.markdown("**① 수치 출처 검사 (규칙 기반)**")
                    if findings:
                        for f in findings:
                            st.markdown(f"- `{', '.join(f['claims'])}` — {f['problem']}  \n  <small>“{html.escape(f['sentence'])}”</small>", unsafe_allow_html=True)
                    else:
                        st.success("출처가 확인되지 않는 수치 주장이 없습니다.")
            with j2:
                with st.container(border=True):
                    st.markdown(f"**② 품질 검수 (LLM 평가 · {q.get('evaluator', '-')})**")
                    if q.get("error"):
                        st.warning(q["error"])
                    else:
                        delta = q["score"] - QUALITY_THRESHOLD
                        st.metric("총점", f"{q['score']} / 100", f"{delta:+d}점 (기준 {QUALITY_THRESHOLD}점)", delta_color="normal")
                        st.markdown(f"어조 **{q['tone']}**/30 · 맥락 **{q['context']}**/40 · 완성도 **{q['completeness']}**/30")
                        type_label = {"evidence": "외부 근거 사칭", "specific": "구체적 사실 단정", "general": "일반 설명(기준 제외)"}
                        if q.get("claims_all"):
                            st.markdown(f"**요청에 없는 내용 {len(q['claims_all'])}건** · 기준 적용 {len(q.get('unsupported_claims', []))}건")
                            for c in q["claims_all"]:
                                color = "chip-gray" if c["type"] == "general" else "chip-purple"
                                st.markdown(f'- <span class="chip {color}">{type_label[c["type"]]}</span><small>“{html.escape(c["text"])}”</small>', unsafe_allow_html=True)
                        if q.get("issues"):
                            st.markdown("**감점 사유**")
                        for issue in q.get("issues", []):
                            st.markdown(f"- {issue}")
                        if q.get("rationale"):
                            st.caption(q["rationale"])
            with st.expander("에이전트 실행 기록"):
                for h in job.get("history", []):
                    st.markdown(f'<div class="step"><code>{h["at"][11:]}</code> <b>{h["step"]}</b> — {html.escape(h["message"])}</div>', unsafe_allow_html=True)

            # --- 승인 시 일어나는 일
            st.markdown("##### 📤 승인하면 일어나는 일")
            st.info(
                f"이 글(또는 아래에서 직접 수정한 본문)이 **구독자 {subscriber_count()}명에게 이메일로 발송**됩니다. "
                f"발송본은 `outbox/{job['request_id']}.md`, 발송 기록은 `outbox/send_log.jsonl`에 남으며 **되돌릴 수 없습니다.**  \n"
                + (f"재작성 요청 시 에이전트가 피드백을 반영해 다시 쓰고, 검사 후 이 대기함으로 다시 돌아옵니다 (남은 재작성 {job['_revisions_left']}회). "
                   if job["_revisions_left"] > 0 else f"재작성 요청은 상한({MAX_REVISIONS}회)에 도달했습니다. 승인·직접 수정 후 승인·반려 중에서 골라 주세요. ")
                + "반려 시 발송하지 않고 종료하며, 사유는 처리 이력에 남습니다.",
                icon="📨",
            )

            # --- 결정
            st.markdown("##### ✅ 담당자 결정")
            with st.form(f"decide_{job['request_id']}"):
                reviewer = st.text_input("담당자 이름", value=st.session_state.get("reviewer", ""))
                comment = st.text_area("코멘트 (재작성 요청·반려 시 필수)", placeholder="예: 78% 수치는 출처를 확인할 수 없으니 빼 주세요.")
                with st.expander("본문을 직접 고친 뒤 승인하기 (선택)"):
                    edited = st.text_area("발송할 본문", value=job["draft"], height=300, key=f"edit_{job['request_id']}_v{job.get('revision', 0)}")  # 버전마다 새 입력칸: 재작성 후 이전 본문이 남지 않게
                b1, b2, b3 = st.columns(3)
                approve = b1.form_submit_button("✅ 승인하고 발송", type="primary", use_container_width=True)
                can_revise = job["_revisions_left"] > 0
                revise = b2.form_submit_button(
                    f"✏️ 재작성 요청 (남은 {job['_revisions_left']}회)" if can_revise else "✏️ 재작성 상한 도달",
                    use_container_width=True, disabled=not can_revise,
                )
                reject = b3.form_submit_button("🚫 반려", use_container_width=True)

            action = "approve" if approve else "revise" if revise else "reject" if reject else None
            if action:
                st.session_state["reviewer"] = reviewer
                if action in ("revise", "reject") and not comment.strip():
                    st.error("재작성 요청이나 반려에는 코멘트를 남겨 주세요.")
                else:
                    with st.spinner("멈춘 지점부터 이어서 실행 중..."):
                        result = service.resume(job["request_id"], action, comment, edited if action == "approve" else "", reviewer)
                    st.session_state["flash"] = {
                        "published": f"📨 `{job['request_id']}` 승인 → 구독자 {result.get('published', {}).get('recipients', 0)}명에게 발송했습니다.",
                        "rejected": f"🚫 `{job['request_id']}` 반려 처리했습니다. 발송하지 않습니다.",
                        "pending_review": f"✏️ `{job['request_id']}` 재작성 완료 → v{result.get('revision', 0) + 1} 초안이 다시 승인 대기함에 들어왔습니다.",
                    }.get(result.get("status"), "처리했습니다.")
                    st.rerun()

    if "flash" in st.session_state:
        st.toast(st.session_state.pop("flash"))

# ------------------------------------------------------------------ 처리 이력

with tab_log:
    st.subheader("처리 이력")
    done = [j for j in jobs if not j["_pending"]]
    auto = sum(1 for j in done if j.get("status") == "published" and j.get("route") == "auto")
    c = st.columns(4)
    c[0].metric("전체 요청", len(jobs))
    c[1].metric("자동 발송", auto)
    c[2].metric("승인 후 발송", sum(1 for j in done if j.get("status") == "published") - auto)
    c[3].metric("승인 대기 / 반려", f"{len(pending)} / {sum(1 for j in done if j.get('status') == 'rejected')}")
    rows = []
    for j in jobs:
        status = "⏸️ 승인 대기" if j["_pending"] else STATUS_LABEL.get(j.get("status"), j.get("status"))
        if j.get("status") == "published":
            status += " (자동)" if j.get("route") == "auto" else " (승인)"
        rows.append({
            "요청 ID": j["request_id"], "주제": j["topic"], "상태": status,
            "검수 점수": j.get("quality", {}).get("score"), "멈춘 이유": short_reason(j) or "-",
            "담당자 결정": {"approve": "승인", "reject": "반려", "revise": "재작성 요청"}.get((j.get("decision") or {}).get("action"), "-"), "버전": j.get("revision", 0) + 1,
        })
    if rows:
        st.dataframe(rows, use_container_width=True, hide_index=True)
        pick = st.selectbox("상세 보기", [j["request_id"] for j in jobs], format_func=lambda r: f"{r} · {next(j['topic'] for j in jobs if j['request_id'] == r)}")
        j = next(x for x in jobs if x["request_id"] == pick)
        d1, d2 = st.columns(2)
        with d1:
            with st.container(border=True, height=420):
                st.markdown(j.get("draft", ""))
        with d2:
            if j.get("decision"):
                st.markdown(f"**담당자 결정**: {j['decision'].get('action')} · {j['decision'].get('reviewer') or '-'} · {j['decision'].get('comment') or ''}")
            for h in j.get("history", []):
                st.markdown(f'<div class="step"><code>{h["at"][11:]}</code> <b>{h["step"]}</b> — {html.escape(h["message"])}</div>', unsafe_allow_html=True)
    else:
        st.caption("아직 처리한 요청이 없습니다.")
