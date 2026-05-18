import json
import subprocess
import sys
import tempfile
from pathlib import Path


RESPONDENT_LABEL = "SMOKE_RESPONDENT"
QUOTE_LABEL = "SMOKE_QUOTE"
EXCLUDE_LABEL = "SMOKE_EXCLUDE"
NEEDS_REVIEW_LABEL = "SMOKE_NEEDS_REVIEW"
MODERATOR_LABEL = "SMOKE_MODERATOR"
OBSERVER_LABEL = "SMOKE_OBSERVER"

SEGMENT_TEXTS = {
    RESPONDENT_LABEL: "Integrated no-ai respondent baseline text",
    QUOTE_LABEL: "Integrated no-ai quote-priority text",
    EXCLUDE_LABEL: "Integrated no-ai excluded text",
    NEEDS_REVIEW_LABEL: "Integrated no-ai needs-review text",
    MODERATOR_LABEL: "Integrated no-ai moderator text",
    OBSERVER_LABEL: "Integrated no-ai observer text",
}


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


def _repo_path_state(repo_root: Path) -> dict[str, bool]:
    return {name: (repo_root / name).exists() for name in ("instance", "uploads", "outputs")}


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    failures = 0
    repo_path_state_before = _repo_path_state(repo_root)

    try:
        import config
        from app import create_app
        from models import db
        from models.analysis import AIAnalysis
        from models.api_usage_log import APIUsageLog  # noqa: F401
        from models.generated_file import GeneratedFile  # noqa: F401
        from models.interview import Interview
        from models.interview_flow import (
            InterviewFlow,
            InterviewFlowQuestion,
            InterviewFlowSection,
        )
        from models.participant import Participant
        from models.project import Project
        from models.quote_candidate import QuoteCandidate  # noqa: F401
        from models.review_item import ReviewItem  # noqa: F401
        from models.segment import Segment
        from models.segment_flag import SegmentFlag
        from models.speaker_assignment import SpeakerAssignment
        from services.integrated_analysis import run_integrated_interview_analysis
    except Exception as e:
        print_result("imports", False, f"{type(e).__name__}: {e}")
        return 1

    original_database_uri = config.DATABASE_URI
    original_upload_dir = config.UPLOAD_DIR
    original_output_dir = config.OUTPUT_DIR

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_root = Path(tmp_dir)
        config.DATABASE_URI = f"sqlite:///{(tmp_root / 'integrated_noai_smoke.db').as_posix()}"
        config.UPLOAD_DIR = str(tmp_root / "uploads")
        config.OUTPUT_DIR = str(tmp_root / "outputs")

        try:
            app = create_app()

            failures += 0 if print_result(
                "temporary database configured",
                config.DATABASE_URI.startswith("sqlite:///")
                and config.DATABASE_URI.endswith("/integrated_noai_smoke.db"),
                config.DATABASE_URI,
            ) else 1
            failures += 0 if print_result(
                "temporary upload/output dirs configured",
                str(tmp_root) in config.UPLOAD_DIR and str(tmp_root) in config.OUTPUT_DIR,
            ) else 1

            with app.app_context():
                project = Project(name="Integrated Analysis No-AI Smoke")
                db.session.add(project)
                db.session.flush()

                participant = Participant(
                    project_id=project.id,
                    participant_code="P01",
                    display_name="Smoke Participant",
                )
                db.session.add(participant)
                db.session.flush()

                flow = InterviewFlow(project_id=project.id, title="Integrated Smoke Flow")
                db.session.add(flow)
                db.session.flush()

                section = InterviewFlowSection(flow_id=flow.id, title="Section 1", seq=1)
                db.session.add(section)
                db.session.flush()

                question = InterviewFlowQuestion(
                    section_id=section.id,
                    question_code="Q1",
                    question_text="Integrated smoke question",
                    is_key_question=True,
                    seq=1,
                )
                question_missing_trace = InterviewFlowQuestion(
                    section_id=section.id,
                    question_code="Q2",
                    question_text="Integrated smoke question without trace",
                    is_key_question=False,
                    seq=2,
                )
                db.session.add_all([question, question_missing_trace])
                db.session.flush()

                interview = Interview(
                    project_id=project.id,
                    participant_id=participant.id,
                    flow_id=flow.id,
                    status="mapped",
                )
                db.session.add(interview)
                db.session.flush()

                labels = [
                    RESPONDENT_LABEL,
                    QUOTE_LABEL,
                    EXCLUDE_LABEL,
                    NEEDS_REVIEW_LABEL,
                    MODERATOR_LABEL,
                    OBSERVER_LABEL,
                ]
                segments_by_label = {}
                for seq, label in enumerate(labels, start=1):
                    seg = Segment(
                        interview_id=interview.id,
                        participant_id=participant.id,
                        speaker_label=label,
                        speaker_role="unknown",
                        start_sec=float(seq * 10),
                        end_sec=float(seq * 10 + 5),
                        text=SEGMENT_TEXTS[label],
                        seq=seq,
                    )
                    db.session.add(seg)
                    segments_by_label[label] = seg
                db.session.flush()

                db.session.add_all([
                    SegmentFlag(segment_id=segments_by_label[QUOTE_LABEL].id, flag_type="quote"),
                    SegmentFlag(segment_id=segments_by_label[EXCLUDE_LABEL].id, flag_type="exclude"),
                    SegmentFlag(segment_id=segments_by_label[NEEDS_REVIEW_LABEL].id, flag_type="needs_review"),
                    SegmentFlag(segment_id=segments_by_label[MODERATOR_LABEL].id, flag_type="quote"),
                    SegmentFlag(segment_id=segments_by_label[OBSERVER_LABEL].id, flag_type="quote"),
                ])

                for label in (RESPONDENT_LABEL, QUOTE_LABEL, EXCLUDE_LABEL, NEEDS_REVIEW_LABEL):
                    db.session.add(
                        SpeakerAssignment(
                            interview_id=interview.id,
                            speaker_label=label,
                            speaker_role="respondent",
                            participant_id=participant.id,
                        )
                    )
                db.session.add(
                    SpeakerAssignment(
                        interview_id=interview.id,
                        speaker_label=MODERATOR_LABEL,
                        speaker_role="moderator",
                        participant_id=None,
                    )
                )
                db.session.add(
                    SpeakerAssignment(
                        interview_id=interview.id,
                        speaker_label=OBSERVER_LABEL,
                        speaker_role="observer",
                        participant_id=None,
                    )
                )

                semantic_payload = {
                    "cluster_summaries": [
                        {
                            "cluster_id": "SC1",
                            "theme": "Traceable respondent evidence",
                            "summary": "Respondent quote and baseline evidence are available.",
                            "evidence_source_segment_ids": [
                                segments_by_label[RESPONDENT_LABEL].id,
                                segments_by_label[QUOTE_LABEL].id,
                                segments_by_label[EXCLUDE_LABEL].id,
                                segments_by_label[NEEDS_REVIEW_LABEL].id,
                                segments_by_label[MODERATOR_LABEL].id,
                                segments_by_label[OBSERVER_LABEL].id,
                            ],
                        }
                    ]
                }
                per_question_payload = {
                    "question_id": question.id,
                    "question_code": question.question_code,
                    "question_text": question.question_text,
                    "findings": [
                        {
                            "point": "Traceable question finding",
                            "evidence_quote": SEGMENT_TEXTS[QUOTE_LABEL],
                        }
                    ],
                    "implications": "Traceable per-question insight can be used as evidence.",
                    "unresolved": "No unresolved smoke question.",
                    "source_segment_ids": [
                        segments_by_label[QUOTE_LABEL].id,
                        segments_by_label[RESPONDENT_LABEL].id,
                    ],
                    "source_segment_quotes": [
                        {"segment_id": segments_by_label[QUOTE_LABEL].id, "text": SEGMENT_TEXTS[QUOTE_LABEL]},
                        {"segment_id": segments_by_label[RESPONDENT_LABEL].id, "text": SEGMENT_TEXTS[RESPONDENT_LABEL]},
                    ],
                    "quote_ids": [f"Q{segments_by_label[QUOTE_LABEL].id}_1"],
                }
                per_question_missing_trace_payload = {
                    "question_id": question_missing_trace.id,
                    "question_code": question_missing_trace.question_code,
                    "question_text": question_missing_trace.question_text,
                    "findings": [
                        {
                            "point": "Supplementary question finding without source ids",
                            "evidence_quote": SEGMENT_TEXTS[QUOTE_LABEL],
                        }
                    ],
                    "implications": "Per-question insight without trace is supplementary.",
                    "unresolved": "No unresolved smoke question.",
                }
                db.session.add(
                    AIAnalysis(
                        project_id=project.id,
                        interview_id=interview.id,
                        analysis_type="semantic_clusters",
                        title="Semantic smoke",
                        content_json=json.dumps(semantic_payload, ensure_ascii=False),
                        model_used="none",
                        status="draft",
                    )
                )
                db.session.add(
                    AIAnalysis(
                        project_id=project.id,
                        interview_id=interview.id,
                        question_id=question.id,
                        analysis_type="per_question",
                        title="Per-question smoke",
                        content_json=json.dumps(per_question_payload, ensure_ascii=False),
                        source_segment_ids=json.dumps(per_question_payload["source_segment_ids"]),
                        quote_ids=json.dumps(per_question_payload["quote_ids"]),
                        model_used="none",
                        status="draft",
                    )
                )
                db.session.add(
                    AIAnalysis(
                        project_id=project.id,
                        interview_id=interview.id,
                        question_id=question_missing_trace.id,
                        analysis_type="per_question",
                        title="Per-question smoke without trace",
                        content_json=json.dumps(per_question_missing_trace_payload, ensure_ascii=False),
                        model_used="none",
                        status="draft",
                    )
                )
                db.session.commit()

                interview_id = interview.id
                expected_ids = {label: seg.id for label, seg in segments_by_label.items()}
                before_text_map = {seg.id: seg.text for seg in segments_by_label.values()}
                before_analysis_total = AIAnalysis.query.count()
                before_analysis_interview = AIAnalysis.query.filter_by(interview_id=interview_id).count()

                failures += 0 if print_result(
                    "temporary integrated fixture created",
                    interview_id is not None and len(expected_ids) == len(labels),
                    f"interview_id={interview_id}, segment_ids={expected_ids}",
                ) else 1

                result = run_integrated_interview_analysis(
                    interview_id=interview_id,
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

                segment_by_id = {
                    seg.id: seg
                    for seg in Segment.query.filter_by(interview_id=interview_id).all()
                }
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

                supporting_quotes = payload.get("supporting_quotes") or []
                support_segment_ids = {
                    int(q.get("segment_id"))
                    for q in supporting_quotes
                    if q.get("segment_id") is not None
                }
                source_ids_set = {int(x) for x in source_ids if str(x).isdigit()}
                quote_id = expected_ids[QUOTE_LABEL]
                respondent_id = expected_ids[RESPONDENT_LABEL]
                exclude_id = expected_ids[EXCLUDE_LABEL]
                needs_review_id = expected_ids[NEEDS_REVIEW_LABEL]
                moderator_id = expected_ids[MODERATOR_LABEL]
                observer_id = expected_ids[OBSERVER_LABEL]

                failures += 0 if print_result(
                    "quote flag segment included",
                    quote_id in support_segment_ids and quote_id in source_ids_set,
                    f"support={sorted(support_segment_ids)}, source={sorted(source_ids_set)}",
                ) else 1
                first_support_id = supporting_quotes[0].get("segment_id") if supporting_quotes else None
                failures += 0 if print_result(
                    "quote flag prioritized",
                    first_support_id == quote_id,
                    f"first_segment_id={first_support_id}, quote_id={quote_id}",
                ) else 1

                excluded_or_review_ids = {exclude_id, needs_review_id}
                failures += 0 if print_result(
                    "exclude and needs_review segments not included",
                    not (excluded_or_review_ids & support_segment_ids)
                    and not (excluded_or_review_ids & source_ids_set),
                    f"blocked={sorted(excluded_or_review_ids)}, support={sorted(support_segment_ids)}, source={sorted(source_ids_set)}",
                ) else 1
                nonrespondent_ids = {moderator_id, observer_id}
                failures += 0 if print_result(
                    "moderator/observer excluded from evidence",
                    not (nonrespondent_ids & support_segment_ids)
                    and not (nonrespondent_ids & source_ids_set),
                    f"blocked={sorted(nonrespondent_ids)}, support={sorted(support_segment_ids)}, source={sorted(source_ids_set)}",
                ) else 1
                failures += 0 if print_result(
                    "respondent fallback remains available",
                    respondent_id in support_segment_ids or respondent_id in source_ids_set,
                    f"respondent_id={respondent_id}",
                ) else 1

                question_insights = payload.get("question_insights") or []
                traceable_question = next((q for q in question_insights if q.get("question_code") == "Q1"), {})
                missing_trace_question = next((q for q in question_insights if q.get("question_code") == "Q2"), {})
                failures += 0 if print_result(
                    "traceable per_question carries evidence ids",
                    traceable_question.get("traceability") == "traceable_source_segment_ids"
                    and quote_id in set(traceable_question.get("evidence_source_segment_ids") or []),
                    f"question={traceable_question}",
                ) else 1
                failures += 0 if print_result(
                    "missing-trace per_question remains supplementary",
                    missing_trace_question.get("traceability") == "supplementary_no_source_segment_ids"
                    and not missing_trace_question.get("evidence_source_segment_ids"),
                    f"question={missing_trace_question}",
                ) else 1
                question_finding = next((f for f in payload.get("key_findings", []) if f.get("finding_id") == "Q1"), {})
                failures += 0 if print_result(
                    "question key finding uses per_question evidence ids",
                    quote_id in set(question_finding.get("evidence_source_segment_ids") or []),
                    f"finding={question_finding}",
                ) else 1

                speaker_summary = payload.get("speaker_assignment_summary") or {}
                failures += 0 if print_result(
                    "speaker_assignment_summary exists",
                    isinstance(speaker_summary, dict)
                    and speaker_summary.get("mapped_label_count") == len(labels)
                    and speaker_summary.get("unresolved_labels") == [],
                    str(speaker_summary),
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

                after_analysis_total = AIAnalysis.query.count()
                after_analysis_interview = AIAnalysis.query.filter_by(interview_id=interview_id).count()
                failures += 0 if print_result(
                    "AIAnalysis count unchanged",
                    after_analysis_total == before_analysis_total
                    and after_analysis_interview == before_analysis_interview,
                    f"before_total={before_analysis_total}, after_total={after_analysis_total}, before_iv={before_analysis_interview}, after_iv={after_analysis_interview}",
                ) else 1

                after_text_map = {
                    seg.id: seg.text
                    for seg in Segment.query.filter_by(interview_id=interview_id).all()
                }
                failures += 0 if print_result(
                    "segment text unchanged",
                    after_text_map == before_text_map,
                ) else 1

                db.session.remove()
                db.engine.dispose()
        finally:
            config.DATABASE_URI = original_database_uri
            config.UPLOAD_DIR = original_upload_dir
            config.OUTPUT_DIR = original_output_dir

    repo_path_state_after = _repo_path_state(repo_root)
    failures += 0 if print_result(
        "repo instance/uploads/outputs state unchanged",
        repo_path_state_after == repo_path_state_before,
        f"before={repo_path_state_before}, after={repo_path_state_after}",
    ) else 1

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
