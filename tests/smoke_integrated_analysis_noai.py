import subprocess
import sys
from pathlib import Path


TARGET_INTERVIEW_ID = 10


def print_result(name: str, ok: bool, detail: str = "") -> bool:
    status = "PASS" if ok else "FAIL"
    if detail:
        print(f"[{status}] {name}: {detail}")
    else:
        print(f"[{status}] {name}")
    return ok


def run_git(repo_root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    failures = 0

    try:
        from app import create_app
        from models.analysis import AIAnalysis
        from models.interview import Interview
        from models.segment import Segment
        from models.segment_flag import SegmentFlag
        from models.speaker_assignment import SpeakerAssignment
        from services.integrated_analysis import run_integrated_interview_analysis
    except Exception as e:
        print_result("imports", False, f"{type(e).__name__}: {e}")
        return 1

    app = create_app()
    with app.app_context():
        interview = Interview.query.get(TARGET_INTERVIEW_ID)
        failures += 0 if print_result(
            "target interview exists",
            interview is not None,
            f"interview_id={TARGET_INTERVIEW_ID}",
        ) else 1
        if interview is None:
            print("\nSummary: FAIL")
            return 1

        before_analysis_total = AIAnalysis.query.count()
        before_analysis_interview = AIAnalysis.query.filter_by(interview_id=TARGET_INTERVIEW_ID).count()

        segments = (
            Segment.query
            .filter_by(interview_id=TARGET_INTERVIEW_ID)
            .order_by(Segment.seq.asc())
            .all()
        )
        before_text_map = {s.id: s.text for s in segments}
        segment_by_id = {s.id: s for s in segments}

        flags_rows = (
            SegmentFlag.query
            .join(Segment, Segment.id == SegmentFlag.segment_id)
            .filter(Segment.interview_id == TARGET_INTERVIEW_ID)
            .all()
        )
        flag_map = {}
        for row in flags_rows:
            flag_map.setdefault(row.segment_id, set()).add(row.flag_type)

        assignment_map = {
            a.speaker_label: a
            for a in SpeakerAssignment.query.filter_by(interview_id=TARGET_INTERVIEW_ID).all()
            if a.speaker_label
        }

        def effective_role(seg):
            a = assignment_map.get(seg.speaker_label or "")
            if a and a.speaker_role:
                return a.speaker_role
            return seg.speaker_role or "unknown"

        # Candidate quote IDs expected to be prioritized if present.
        expected_quote_ids = []
        for seg in segments:
            flags = flag_map.get(seg.id, set())
            role = effective_role(seg)
            if role in ("moderator", "observer"):
                continue
            if "exclude" in flags:
                continue
            if "needs_review" in flags:
                continue
            if "quote" in flags:
                expected_quote_ids.append(seg.id)

        result = run_integrated_interview_analysis(
            interview_id=TARGET_INTERVIEW_ID,
            no_ai=True,
            save=False,
            max_quotes=20,
            include_needs_review=False,
        )
        payload = result.get("payload") or {}

        failures += 0 if print_result("result ok", bool(result.get("ok"))) else 1
        failures += 0 if print_result(
            "analysis_type",
            payload.get("analysis_type") == "integrated_interview_analysis",
            f"value={payload.get('analysis_type')}",
        ) else 1
        failures += 0 if print_result(
            "mode no_ai_dry_run",
            payload.get("mode") == "no_ai_dry_run",
            f"value={payload.get('mode')}",
        ) else 1

        required_keys = {
            "analysis_type",
            "interview_id",
            "mode",
            "key_findings",
            "supporting_quotes",
            "participant_insights",
            "question_insights",
            "semantic_cluster_insights",
            "product_or_brand_mentions",
            "implications",
            "unresolved_questions",
            "cautions",
            "evidence_map",
            "source_segment_ids",
            "source_segment_quotes",
            "flag_summary",
            "speaker_assignment_summary",
            "models",
            "created_at",
        }
        missing = sorted(list(required_keys - set(payload.keys())))
        failures += 0 if print_result(
            "payload keys",
            len(missing) == 0,
            f"missing={missing}",
        ) else 1

        # source quotes must be exact Segment.text
        source_ids = payload.get("source_segment_ids") or []
        source_quotes = payload.get("source_segment_quotes") or []
        exact_match_count = 0
        for sid, quote in zip(source_ids, source_quotes):
            seg = segment_by_id.get(int(sid))
            if seg and seg.text == quote:
                exact_match_count += 1
        failures += 0 if print_result(
            "source quotes exact match",
            exact_match_count == len(source_quotes),
            f"exact={exact_match_count}/{len(source_quotes)}",
        ) else 1

        # exclude flag segments should not appear in supporting quotes/source ids
        excluded_ids = {sid for sid, flags in flag_map.items() if "exclude" in flags}
        support_segment_ids = {int(q.get("segment_id")) for q in (payload.get("supporting_quotes") or []) if q.get("segment_id") is not None}
        source_ids_set = {int(x) for x in source_ids if str(x).isdigit()}
        exclude_included = (excluded_ids & support_segment_ids) or (excluded_ids & source_ids_set)
        failures += 0 if print_result(
            "exclude segments not included",
            len(exclude_included) == 0,
            f"found={sorted(list(exclude_included))}",
        ) else 1

        # quote-priority check (conditional)
        supporting_quotes = payload.get("supporting_quotes") or []
        quote_priority_ok = True
        if expected_quote_ids:
            expected_set = set(expected_quote_ids)
            seen_non_quote = False
            for item in supporting_quotes:
                sid = item.get("segment_id")
                if sid in expected_set and seen_non_quote:
                    quote_priority_ok = False
                    break
                if sid not in expected_set:
                    seen_non_quote = True
        failures += 0 if print_result(
            "quote priority if quote flags exist",
            quote_priority_ok,
            f"quote_candidates={len(expected_quote_ids)}",
        ) else 1

        speaker_summary = payload.get("speaker_assignment_summary") or {}
        failures += 0 if print_result(
            "speaker_assignment_summary exists",
            isinstance(speaker_summary, dict) and "unresolved_labels" in speaker_summary,
        ) else 1

        failures += 0 if print_result(
            "api call count is zero",
            result.get("api_call_count") == 0,
            f"value={result.get('api_call_count')}",
        ) else 1
        failures += 0 if print_result(
            "db update not performed",
            result.get("db_update_performed") is False,
            f"value={result.get('db_update_performed')}",
        ) else 1

        # Post-check: no DB writes and no Segment.text changes
        after_analysis_total = AIAnalysis.query.count()
        after_analysis_interview = AIAnalysis.query.filter_by(interview_id=TARGET_INTERVIEW_ID).count()
        failures += 0 if print_result(
            "AIAnalysis count unchanged",
            after_analysis_total == before_analysis_total and after_analysis_interview == before_analysis_interview,
            f"before_total={before_analysis_total}, after_total={after_analysis_total}, before_iv={before_analysis_interview}, after_iv={after_analysis_interview}",
        ) else 1

        segments_after = Segment.query.filter_by(interview_id=TARGET_INTERVIEW_ID).all()
        unchanged = all(before_text_map.get(s.id) == s.text for s in segments_after)
        failures += 0 if print_result("segment text unchanged", unchanged) else 1

    status_proc = run_git(repo_root, "status", "--short")
    if status_proc.returncode == 0:
        out = status_proc.stdout.strip()
        print("git status --short:")
        print(out if out else "(clean)")
    else:
        failures += 0 if print_result(
            "git status --short",
            False,
            (status_proc.stderr or status_proc.stdout or "unknown git error").strip(),
        ) else 1

    if failures == 0:
        print("\nSummary: PASS")
        return 0
    print(f"\nSummary: FAIL ({failures} checks failed)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
