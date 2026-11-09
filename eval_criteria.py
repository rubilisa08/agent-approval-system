"""멈춤 기준 비교표: 기준 조합마다 개입률 · 놓침 · 헛멈춤을 센다 (노드 3).

  python eval_criteria.py            # .env의 LLM으로 평가 (키가 없으면 offline)
  python eval_criteria.py --runs 2   # 같은 데이터를 여러 번 돌려 LLM 판단의 흔들림까지 확인

eval_set.py의 고정 초안 10건에 실제 파이프라인의 check_facts / evaluate_quality 노드를 그대로 돌리고,
그 결과에 기준 조합을 하나씩 적용해 '멈췄는가'를 정답(should_stop)과 비교한다.
결과는 화면에 출력하고 docs/criteria_eval.md에 저장한다.
"""
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

import llm
from criteria import QUALITY_THRESHOLD
from eval_set import EVAL_SET
from graph import check_facts, evaluate_quality

# 기준 판정: 평가 결과 하나(r)에서 각 기준에 걸렸는지
CHECKS = {
    "①": lambda r: bool(r["fact_findings"]),                            # 숫자 출처 누락
    "②": lambda r: bool(r["quality"].get("unsupported_claims")),        # 근거 없는 주장
    "③": lambda r: (r["quality"].get("score") or 0) < QUALITY_THRESHOLD,  # 검수 점수 미달
    "④": lambda r: bool(r["quality"].get("error")),                     # 품질 평가 실패
}
COMBOS = [
    ("기준 없음 (전부 자동)", lambda r: False),
    ("① 숫자 출처 누락만", lambda r: CHECKS["①"](r)),
    ("③ 검수 점수 미달만", lambda r: CHECKS["③"](r)),
    ("①+③ (처음 설계한 두 기준)", lambda r: CHECKS["①"](r) or CHECKS["③"](r)),
    ("①+②+③+④ (현재)", lambda r: any(f(r) for f in CHECKS.values())),
    ("전부 멈춤", lambda r: True),
]


def evaluate_once() -> list[dict]:
    rows = []
    for case in EVAL_SET:
        state = {k: case[k] for k in ("topic", "audience", "sources", "key_points", "draft")}
        facts = check_facts(state)["fact_findings"]
        quality = evaluate_quality({**state, "fact_findings": facts})["quality"]
        rows.append({"case": case, "fact_findings": facts, "quality": quality})
    return rows


def measure(rows: list[dict], rule) -> tuple[int, list[str], list[str]]:
    stopped, missed, false_stop = 0, [], []
    for r in rows:
        did = rule(r)
        stopped += did
        if did and not r["case"]["should_stop"]:
            false_stop.append(r["case"]["id"])
        if not did and r["case"]["should_stop"]:
            missed.append(r["case"]["id"])
    return stopped, missed, false_stop


def render(all_runs: list[list[dict]]) -> str:
    n = len(EVAL_SET)
    out = [f"# 멈춤 기준 비교표", "",
           f"- 실행: {datetime.now():%Y-%m-%d %H:%M} · 평가자: {llm.provider_label()} · 기준 점수 {QUALITY_THRESHOLD}점",
           f"- 데이터: `eval_set.py` 고정 초안 {n}건 (사람이 봐야 함 {sum(c['should_stop'] for c in EVAL_SET)}건 / 안 봐도 됨 {sum(not c['should_stop'] for c in EVAL_SET)}건)",
           f"- 반복: {len(all_runs)}회", ""]
    for i, rows in enumerate(all_runs, 1):
        out += [f"## 실행 {i}", "", "| 멈춤 기준 | 개입률 | 놓침 | 헛멈춤 |", "|---|---|---|---|"]
        for name, rule in COMBOS:
            s, m, f = measure(rows, rule)
            out.append(f"| {name} | {s}/{n} | {len(m)}건{' (' + ', '.join(m) + ')' if m else ''} | {len(f)}건{' (' + ', '.join(f) + ')' if f else ''} |")
        out += ["", "<details><summary>건별 판정</summary>", "",
                "| 건 | 유형 | 정답 | ① 출처 누락 | ② 근거 없는 주장 | ③ 점수 | 현재 기준 |", "|---|---|---|---|---|---|---|"]
        for r in rows:
            c, q = r["case"], r["quality"]
            now = any(f(r) for f in CHECKS.values())
            mark = "✅" if now == c["should_stop"] else "❌"
            out.append(
                f"| {c['id']} | {c['kind']} | {'멈춤' if c['should_stop'] else '자동'} | {len(r['fact_findings'])}건 | "
                f"{len(q.get('unsupported_claims') or [])}건 | {q.get('score') if not q.get('error') else '오류'} | {'멈춤' if now else '자동'} {mark} |"
            )
        out += ["", "</details>", ""]
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--out", default=str(Path(__file__).parent / "docs" / "criteria_eval.md"))
    args = ap.parse_args()
    runs = [evaluate_once() for _ in range(args.runs)]
    md = render(runs)
    Path(args.out).parent.mkdir(exist_ok=True)
    Path(args.out).write_text(md, encoding="utf-8")
    print(md)


if __name__ == "__main__":
    main()
