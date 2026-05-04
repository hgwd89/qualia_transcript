import os
import subprocess
import sys
from pathlib import Path


def print_result(name: str, ok: bool, detail: str = "") -> bool:
    status = "PASS" if ok else "FAIL"
    if detail:
        print(f"[{status}] {name}: {detail}")
    else:
        print(f"[{status}] {name}")
    return ok


def run_git_status(repo_root: Path, label: str) -> bool:
    proc = subprocess.run(
        ["git", "status", "--short"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    print(f"--- git status --short ({label}) ---")
    print(proc.stdout.rstrip())
    if proc.stderr.strip():
        print(proc.stderr.strip())
    print("--- end ---")
    return proc.returncode == 0


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    failures = 0
    failures += 0 if print_result(
        "git status --short before", run_git_status(repo_root, "before")
    ) else 1

    # このプロセス限定でプロキシを外す（値は表示しない）
    for key in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]:
        os.environ.pop(key, None)

    try:
        from app import create_app
        from models.interview import Interview
        from models.segment import Segment, UtteranceMapping
        from models.interview_flow import InterviewFlowQuestion
        import services.mapper as mapper
    except Exception as e:
        failures += 0 if print_result("imports", False, f"{type(e).__name__}: {e}") else 1
        print("\nSummary: FAIL")
        return 1

    app = create_app()
    with app.app_context():
        interview_id = 4
        segment_id = 40
        expected_text = "大学の時に上京しました。"
        expected_q2_text = "いつ上京しましたか？"

        interview = Interview.query.get(interview_id)
        failures += 0 if print_result("interview_id=4 exists", interview is not None) else 1
        if interview is None:
            print("\nSummary: FAIL")
            return 1

        failures += 0 if print_result(
            "flow_id=1",
            interview.flow_id == 1,
            f"flow_id={interview.flow_id}",
        ) else 1

        segment = Segment.query.get(segment_id)
        failures += 0 if print_result("segment_id=40 exists", segment is not None) else 1
        if segment is None:
            print("\nSummary: FAIL")
            return 1

        failures += 0 if print_result(
            "segment text matches",
            (segment.text or "").strip() == expected_text,
            f"text={(segment.text or '').strip()}",
        ) else 1
        failures += 0 if print_result(
            "segment speaker_role=respondent",
            segment.speaker_role == "respondent",
            f"speaker_role={segment.speaker_role}",
        ) else 1

        q2 = InterviewFlowQuestion.query.filter_by(
            question_code="Q2", question_text=expected_q2_text
        ).first()
        q2_ok = q2 is not None and q2.section and q2.section.flow_id == 1
        failures += 0 if print_result(
            "Q2 exists in flow_id=1",
            q2_ok,
            f"question_id={q2.id if q2 else None}",
        ) else 1
        if not q2_ok:
            print("\nSummary: FAIL")
            return 1

        # 実行前に manual delete 方針を明示（run_mapping内の既存削除ロジックを利用）
        print("[INFO] Existing mappings manual delete: NO")
        print("[INFO] run_mapping() internal replacement will be used.")

        seg_ids = [s.id for s in Segment.query.filter_by(interview_id=interview_id).all()]
        before_count = (
            UtteranceMapping.query.filter(UtteranceMapping.segment_id.in_(seg_ids)).count()
            if seg_ids else 0
        )
        print(f"[INFO] mapping_count_before={before_count}")

        counter = {"n": 0}
        original_call = mapper.call_structured

        def wrapped_call(*args, **kwargs):
            counter["n"] += 1
            return original_call(*args, **kwargs)

        mapper.call_structured = wrapped_call

        run_ok = True
        run_error = ""
        try:
            mapper.run_mapping(interview_id)
        except Exception as e:
            run_ok = False
            run_error = f"{type(e).__name__}: {e}"
        finally:
            mapper.call_structured = original_call

        failures += 0 if print_result("run_mapping(4)", run_ok, run_error) else 1
        failures += 0 if print_result(
            "OpenAI API call count == 1",
            counter["n"] == 1,
            f"count={counter['n']}",
        ) else 1

        seg_ids_after = [s.id for s in Segment.query.filter_by(interview_id=interview_id).all()]
        after_count = (
            UtteranceMapping.query.filter(UtteranceMapping.segment_id.in_(seg_ids_after)).count()
            if seg_ids_after else 0
        )
        failures += 0 if print_result(
            "mapping_count >= 1",
            after_count >= 1,
            f"count={after_count}",
        ) else 1

        latest_for_segment = (
            UtteranceMapping.query.filter_by(segment_id=segment_id)
            .order_by(UtteranceMapping.id.desc())
            .first()
        )
        failures += 0 if print_result(
            "latest mapping exists for segment_id=40",
            latest_for_segment is not None,
        ) else 1

        if latest_for_segment is not None:
            failures += 0 if print_result(
                "question_id mapped to Q2",
                latest_for_segment.question_id == q2.id,
                f"question_id={latest_for_segment.question_id}, expected={q2.id}",
            ) else 1
            failures += 0 if print_result(
                "is_unclassified=False",
                latest_for_segment.is_unclassified is False,
                f"is_unclassified={latest_for_segment.is_unclassified}",
            ) else 1
            conf = latest_for_segment.confidence if latest_for_segment.confidence is not None else -1
            failures += 0 if print_result(
                "confidence > 0",
                conf > 0,
                f"confidence={latest_for_segment.confidence}",
            ) else 1

    failures += 0 if print_result(
        "git status --short after", run_git_status(repo_root, "after")
    ) else 1

    if failures == 0:
        print("\nSummary: PASS")
        return 0
    print(f"\nSummary: FAIL ({failures} checks failed)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
