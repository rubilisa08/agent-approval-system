"""터미널에서 HITL 흐름을 확인하는 CLI. 프로세스를 나눠 실행해도 체크포인터에서 이어진다.

  python cli.py samples                 # 샘플 6건 실행
  python cli.py list                    # 작업 목록
  python cli.py pending                 # 승인 대기 건만 (기한 초과 건부터)
  python cli.py approve <id> [이름]      # 승인 → 발송
  python cli.py revise  <id> "<피드백>"  # 재작성 요청 → 다시 승인 대기
  python cli.py reject  <id> "<사유>"    # 반려
"""
import sys

import service
from samples import SAMPLES


def show(j):
    state = ("OVERDUE" if j["_overdue"] else "PENDING") if j["_pending"] else j.get("status")
    reasons = ", ".join(r["label"] for r in j.get("reasons", [])) or "-"
    wait = f" wait={service.format_age(j['_pending_days'])} revise_left={j['_revisions_left']}" if j["_pending"] else ""
    print(f"{j['request_id']}  {state:<10} score={j.get('quality', {}).get('score')!s:<4} reasons={reasons}{wait}  | {j['topic']}")


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "list"
    if cmd == "samples":
        for s in SAMPLES.values():
            show(service.get(service.submit(s["topic"], s["key_points"], s["sources"].splitlines(), s["audience"])))
    elif cmd == "list":
        for j in service.list_jobs():
            show(j)
    elif cmd == "pending":  # 대기 건만, 기한을 넘긴 건부터
        for j in service.pending_jobs():
            show(j)
    elif cmd in ("approve", "revise", "reject"):
        tid = argv[2]
        text = argv[3] if len(argv) > 3 else ""
        try:
            if cmd == "approve":
                show(service.resume(tid, "approve", reviewer=text or "cli"))
            else:
                show(service.resume(tid, cmd, comment=text, reviewer="cli"))
        except ValueError as e:
            print("거절:", e)
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv)
