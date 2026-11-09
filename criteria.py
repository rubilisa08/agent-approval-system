"""사람의 승인을 받을지 정하는 기준. 모두 '계산 가능한' 형태로 둔다.

기준 1. NUMERIC_NO_SOURCE (규칙 기반)
    본문에 수치 주장(숫자 + %/명/원/배 등 단위)이 있는데
      - 그 문장에 인용 표시([n] 또는 '(출처: ...)')가 없거나
      - 인용 번호가 참고자료 목록에 없거나
      - 참고자료의 URL이 요청자가 준 출처 목록에 없는(=에이전트가 만들어 낸) 경우
    한 건이라도 있으면 사람에게 보낸다.

기준 2. LOW_QUALITY (LLM 평가)
    평가 노드가 매긴 어조(30) + 맥락 적합성(40) + 완성도(30) = 100점 만점 점수가
    QUALITY_THRESHOLD(기본 80) 미만이면 사람에게 보낸다.

기준 3. EVAL_ERROR (안전장치)
    평가 노드가 실패해 점수를 못 매긴 경우. '모르면 멈춘다'.

기준 3-1. UNSUPPORTED_CLAIM (LLM 평가)
    평가 노드가 '핵심 내용·제공된 출처로 뒷받침되지 않는 주장'을 1건 이상 찾으면 사람에게 보낸다.
    숫자가 없는 지어낸 주장(예: "전문가 테스트를 거쳤다")은 기준 1의 규칙이 잡지 못하기 때문이다.
    점수와 달리 '건수'로 판단하므로, 평가 LLM이 점수를 후하게 줘도 이 기준은 따로 작동한다.

기준 4. REVISION_CONFIRM (재작성본 확인)
    담당자가 '재작성 요청'을 보낸 글은 다시 검사를 통과하더라도 담당자에게 되돌린다.
    수정 요청이 의도대로 반영됐는지는 요청한 사람만 판단할 수 있기 때문이다.

기준별 이유와 위험, 자동 처리 건을 사람이 보지 않아도 되는 이유는 README.md에 정리했다.
"""
from __future__ import annotations

import os
import re

QUALITY_THRESHOLD = int(os.getenv("QUALITY_THRESHOLD", "80"))

REASON_LABELS = {
    "NUMERIC_NO_SOURCE": "숫자 출처 누락",
    "LOW_QUALITY": "검수 점수 미달",
    "EVAL_ERROR": "품질 평가 실패",
    "UNSUPPORTED_CLAIM": "근거 없는 주장",
    "REVISION_CONFIRM": "재작성본 확인",
}

# ---------------------------------------------------------------- 기준 1: 수치 출처 검사

# 통계·사실 주장으로 볼 수 있는 '숫자 + 단위'. '5가지', '1단계', 날짜(9일) 같은 표현은 제외한다.
UNIT = r"(?:%p|%|퍼센트|배|명|원|달러|억|만|천|조|건|개국|개사|곳|위|개월|시간|년)"
NUMERIC_CLAIM = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(" + UNIT + r")")
CITE_MARK = re.compile(r"\[(\d+)\]")
INLINE_SOURCE = re.compile(r"\(출처\s*[:：]\s*([^)]+)\)")
URL = re.compile(r"https?://[^\s)\]>]+")
REF_HEADING = re.compile(r"^\s*#{0,6}\s*(참고\s*자료|참고\s*문헌|출처|References?)\s*:?\s*$", re.I)


def _norm_url(u: str) -> str:
    return u.strip().rstrip("/.,").lower()


def split_body_and_refs(post: str) -> tuple[str, dict[int, str]]:
    """본문과 참고자료 목록({번호: 항목 텍스트})을 나눈다."""
    lines = post.splitlines()
    for i, line in enumerate(lines):
        if REF_HEADING.match(line):
            body, ref_lines = lines[:i], lines[i + 1 :]
            break
    else:
        return post, {}
    refs: dict[int, str] = {}
    for line in ref_lines:
        m = re.match(r"^\s*[-*]?\s*\[?(\d+)[\].)]\s*(.+)$", line)
        if m and m.group(2).strip():
            refs[int(m.group(1))] = m.group(2).strip()
    return "\n".join(body), refs


def _sentences(body: str) -> list[str]:
    out = []
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):  # 제목 줄은 본문에서 반복되므로 건너뛴다
            continue
        line = re.sub(r"^[-*]\s+", "", line)
        out.extend(s.strip() for s in re.split(r"(?<=[.!?。])\s+", line) if s.strip())
    return out


def _claims(sentence: str) -> list[str]:
    found = []
    for m in NUMERIC_CLAIM.finditer(CITE_MARK.sub("", sentence)):
        num, unit = m.group(1), m.group(2)
        if unit == "년" and re.fullmatch(r"(19|20)\d\d", num.replace(",", "")):
            continue  # '2026년' 같은 연도는 수치 주장이 아니다
        found.append(m.group(0))
    return found


def check_numeric_sources(post: str, allowed_sources: list[str]) -> list[dict]:
    """출처가 확인되지 않는 수치 주장 목록을 돌려준다. 빈 리스트면 통과."""
    body, refs = split_body_and_refs(post)
    allowed = {_norm_url(u) for s in allowed_sources for u in URL.findall(s)}
    findings = []
    for sent in _sentences(body):
        claims = _claims(sent)
        if not claims:
            continue
        marks = [int(n) for n in CITE_MARK.findall(sent)]
        inline = INLINE_SOURCE.findall(sent)
        problem = None
        if not marks and not inline:
            problem = "문장에 인용 표시가 없음"
        else:
            for n in marks:
                if n not in refs:
                    problem = f"인용 [{n}]이 참고자료 목록에 없음"
                    break
                urls = URL.findall(refs[n])
                if urls and any(_norm_url(u) not in allowed for u in urls):
                    problem = f"인용 [{n}]의 URL이 요청에서 받은 출처가 아님(생성된 출처 의심)"
                    break
            if not problem:
                for src in inline:
                    urls = URL.findall(src)
                    if urls and any(_norm_url(u) not in allowed for u in urls):
                        problem = "인라인 출처 URL이 요청에서 받은 출처가 아님"
                        break
        if problem:
            findings.append({"sentence": sent, "claims": claims, "problem": problem})
    return findings


# ---------------------------------------------------------------- 기준 2: 품질 점수 (offline 대체 채점)

HYPE_WORDS = ["대박", "무조건", "역대급", "미쳤", "충격", "꼭 사", "강추", "레전드", "100% 보장", "지금 당장"]


def offline_quality_score(topic: str, post: str) -> dict:
    """API 키가 없을 때 LLM 평가를 대신하는 규칙 기반 채점. LLM 평가와 같은 3개 항목·배점을 쓴다."""
    body, _ = split_body_and_refs(post)
    issues = []

    tone = 30
    hype = [w for w in HYPE_WORDS if w in body]
    if hype:
        tone -= min(18, 6 * len(hype))
        issues.append(f"과장·선동 표현: {', '.join(hype)}")
    if body.count("!") > 3:
        tone -= 6
        issues.append(f"느낌표 과다({body.count('!')}개)")
    if re.search(r"ㅋㅋ|ㅎㅎ|ㅠㅠ", body):
        tone -= 6
        issues.append("구독 메일에 맞지 않는 구어체")
    tone = max(tone, 0)

    keywords = [w for w in re.findall(r"[가-힣A-Za-z0-9]{2,}", topic) if w not in ("위한", "가지", "이번", "대한")]
    hit = [w for w in keywords if w[:2] in body]
    context = round(40 * (len(hit) / len(keywords))) if keywords else 30
    context = max(min(context, 40), 10)
    if keywords and len(hit) < len(keywords):
        issues.append(f"주제 키워드 미반영: {', '.join(w for w in keywords if w not in hit)}")

    text_len = len(re.sub(r"\s", "", body))
    paragraphs = [p for p in re.split(r"\n\s*\n", body) if p.strip()]
    completeness = 0
    completeness += 5 if body.lstrip().startswith("#") else 0
    completeness += 15 if text_len >= 300 else 8 if text_len >= 180 else 0
    completeness += 5 if len(paragraphs) >= 4 else 0
    completeness += 5 if re.search(r"정리|요약|마무리|결론", body) else 0
    if text_len < 300:
        issues.append(f"분량 부족(공백 제외 {text_len}자, 권장 300자 이상)")
    if len(paragraphs) < 4:
        issues.append("문단 구성이 단조로움")

    score = tone + context + completeness
    return {
        "score": score,
        "tone": tone,
        "context": context,
        "completeness": completeness,
        "issues": issues,
        "rationale": "offline 규칙 채점(과장 표현·느낌표·주제 키워드·분량·구성)으로 LLM 평가를 대신함",
        "evaluator": "offline-rules",
    }


# ---------------------------------------------------------------- 게이트: 사람에게 보낼지 결정


def review_reasons(fact_findings: list[dict], quality: dict, revision: int = 0) -> list[dict]:
    """승인 기준에 걸린 이유 목록. 비어 있으면 사람 없이 자동 발행한다."""
    reasons = []
    if revision > 0:
        reasons.append({
            "code": "REVISION_CONFIRM",
            "label": REASON_LABELS["REVISION_CONFIRM"],
            "detail": f"담당자 요청으로 다시 쓴 v{revision + 1} 초안 — 요청이 반영됐는지 확인 필요",
        })
    if fact_findings:
        reasons.append({
            "code": "NUMERIC_NO_SOURCE",
            "label": REASON_LABELS["NUMERIC_NO_SOURCE"],
            "detail": f"출처가 확인되지 않는 수치 주장 {len(fact_findings)}건",
        })
    if quality.get("error"):
        reasons.append({
            "code": "EVAL_ERROR",
            "label": REASON_LABELS["EVAL_ERROR"],
            "detail": f"평가 노드 오류: {quality['error']}",
        })
        return reasons
    if quality.get("unsupported_claims"):
        reasons.append({
            "code": "UNSUPPORTED_CLAIM",
            "label": REASON_LABELS["UNSUPPORTED_CLAIM"],
            "detail": f"요청 내용·출처로 뒷받침되지 않는 주장 {len(quality['unsupported_claims'])}건",
        })
    if quality.get("score", 0) < QUALITY_THRESHOLD:
        reasons.append({
            "code": "LOW_QUALITY",
            "label": REASON_LABELS["LOW_QUALITY"],
            "detail": f"검수 점수 {quality['score']}점 (기준 {QUALITY_THRESHOLD}점)",
        })
    return reasons
