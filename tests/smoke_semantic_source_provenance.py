from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path


def check(name: str, ok: bool, detail: str = "") -> int:
    status = "PASS" if ok else "FAIL"
    suffix = f": {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return 0 if ok else 1


def main() -> int:
    failures = 0
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    import config

    original = {
        "DATABASE_URI": config.DATABASE_URI,
        "UPLOAD_DIR": config.UPLOAD_DIR,
        "OUTPUT_DIR": config.OUTPUT_DIR,
    }
    app = None

    with tempfile.TemporaryDirectory(prefix="qualia_semantic_provenance_") as tmp:
        root = Path(tmp)
        config.DATABASE_URI = f"sqlite:///{(root / 'semantic-provenance.db').as_posix()}"
        config.UPLOAD_DIR = str(root / "uploads")
        config.OUTPUT_DIR = str(root / "outputs")

        try:
            import numpy as np

            from app import create_app
            from models import db
            from models.analysis import AIAnalysis
            from models.interview import Interview
            from models.participant import Participant
            from models.project import Project
            from models.segment import Segment
            from services.fragmentation import collect_candidate_segments
            from services.semantic_source_provenance import (
                SEMANTIC_PROVENANCE_KEY,
                SEMANTIC_REQUEST_KEY,
                SemanticSourceProvenanceError,
                semantic_analysis_source_provenance_status,
            )
            import services.semantic_analysis as semantic_analysis

            app = create_app()
            app.config["TESTING"] = True

            with app.app_context():
                project = Project(name="Semantic provenance smoke")
                db.session.add(project)
                db.session.flush()
                participant = Participant(
                    project_id=project.id,
                    participant_code="P01",
                    display_name="Participant 1",
                )
                db.session.add(participant)
                db.session.flush()
                interview = Interview(
                    project_id=project.id,
                    participant_id=participant.id,
                    status="mapped",
                )
                db.session.add(interview)
                db.session.flush()

                first = Segment(
                    interview_id=interview.id,
                    participant_id=participant.id,
                    speaker_label="SPEAKER_01",
                    speaker_role="respondent",
                    text="最初の回答は毎日安心して使えることが重要です。",
                    start_sec=0.0,
                    end_sec=2.0,
                    seq=0,
                )
                second = Segment(
                    interview_id=interview.id,
                    participant_id=participant.id,
                    speaker_label="SPEAKER_01",
                    speaker_role="respondent",
                    text="次の回答では価格よりも使用感を重視しています。",
                    start_sec=2.0,
                    end_sec=4.0,
                    seq=1,
                )
                db.session.add_all([first, second])
                db.session.commit()

                original_embed = semantic_analysis.embed_fragments
                original_cluster = semantic_analysis.cluster_embeddings
                semantic_analysis.embed_fragments = lambda fragments: np.ones(
                    (len(fragments), 3), dtype=np.float32
                )
                semantic_analysis.cluster_embeddings = lambda embeddings: [0] * int(embeddings.shape[0])
                try:
                    result = semantic_analysis.run_semantic_cluster_analysis(
                        interview.id,
                        save=True,
                        max_segments=None,
                        no_ai=True,
                        result_write_guard=lambda: None,
                    )
                finally:
                    semantic_analysis.embed_fragments = original_embed
                    semantic_analysis.cluster_embeddings = original_cluster

                analysis = db.session.get(AIAnalysis, int(result["saved_analysis_id"]))
                content = json.loads(analysis.content_json or "{}")
                current, reason = semantic_analysis_source_provenance_status(analysis)
                failures += check(
                    "saved semantic analysis persists source fingerprint and request",
                    bool(
                        current
                        and not reason
                        and isinstance(content.get(SEMANTIC_PROVENANCE_KEY), dict)
                        and content.get(SEMANTIC_REQUEST_KEY)
                        == {"max_segments": None, "no_ai": True}
                    ),
                    f"current={current} reason={reason} keys={sorted(content.keys())}",
                )

                first.text = "生成後に変更された回答です。"
                db.session.commit()
                current, reason = semantic_analysis_source_provenance_status(analysis)
                failures += check(
                    "semantic result becomes stale after canonical source text changes",
                    not current and "changed after generation" in reason,
                    f"current={current} reason={reason}",
                )

                first.text = "最初の回答は毎日安心して使えることが重要です。"
                db.session.commit()

                semantic_analysis.embed_fragments = lambda fragments: np.ones(
                    (len(fragments), 3), dtype=np.float32
                )
                semantic_analysis.cluster_embeddings = lambda embeddings: [0] * int(embeddings.shape[0])
                try:
                    prefix_result = semantic_analysis.run_semantic_cluster_analysis(
                        interview.id,
                        save=True,
                        max_segments=1,
                        no_ai=True,
                        result_write_guard=lambda: None,
                    )
                finally:
                    semantic_analysis.embed_fragments = original_embed
                    semantic_analysis.cluster_embeddings = original_cluster

                prefix_analysis = db.session.get(AIAnalysis, int(prefix_result["saved_analysis_id"]))
                second.text = "prefix外の回答だけを変更しました。"
                db.session.commit()
                current, reason = semantic_analysis_source_provenance_status(prefix_analysis)
                failures += check(
                    "max_segments provenance scopes currentness to the exact consumed prefix",
                    current and not reason,
                    f"current={current} reason={reason}",
                )

                # Equal seq values are historical-data-compatible. Candidate order
                # must still be deterministic because max_segments selects a prefix.
                tie_a = Segment(
                    interview_id=interview.id,
                    participant_id=participant.id,
                    speaker_label="SPEAKER_01",
                    speaker_role="respondent",
                    text="同順位Aの十分に長い回答テキストです。",
                    seq=5,
                )
                tie_b = Segment(
                    interview_id=interview.id,
                    participant_id=participant.id,
                    speaker_label="SPEAKER_01",
                    speaker_role="respondent",
                    text="同順位Bの十分に長い回答テキストです。",
                    seq=5,
                )
                db.session.add_all([tie_b, tie_a])
                db.session.commit()
                ordered_ids = [
                    int(item["source_segment_ids"][0])
                    for item in collect_candidate_segments(interview.id)
                    if int(item["source_segment_ids"][0]) in {int(tie_a.id), int(tie_b.id)}
                ]
                failures += check(
                    "semantic candidate ordering is deterministic for duplicate seq",
                    ordered_ids == sorted([int(tie_a.id), int(tie_b.id)]),
                    f"ordered_ids={ordered_ids}",
                )

                # Defense in depth: even if a caller bypasses the normal active-job
                # input guard, a mutation during provider work must not be committed
                # with the old source fingerprint.
                bypass_interview = Interview(
                    project_id=project.id,
                    participant_id=participant.id,
                    status="mapped",
                )
                db.session.add(bypass_interview)
                db.session.flush()
                bypass_segment = Segment(
                    interview_id=bypass_interview.id,
                    participant_id=participant.id,
                    speaker_label="SPEAKER_01",
                    speaker_role="respondent",
                    text="プロバイダ呼び出し前の十分に長い回答テキストです。",
                    seq=0,
                )
                db.session.add(bypass_segment)
                db.session.commit()

                def mutate_during_embed(fragments):
                    current_segment = db.session.get(Segment, int(bypass_segment.id))
                    current_segment.text = "プロバイダ処理中に差し替えられた回答テキストです。"
                    db.session.commit()
                    return np.ones((len(fragments), 3), dtype=np.float32)

                semantic_analysis.embed_fragments = mutate_during_embed
                semantic_analysis.cluster_embeddings = lambda embeddings: [0] * int(embeddings.shape[0])
                raised = False
                try:
                    semantic_analysis.run_semantic_cluster_analysis(
                        bypass_interview.id,
                        save=True,
                        no_ai=True,
                        result_write_guard=lambda: None,
                    )
                except SemanticSourceProvenanceError as exc:
                    raised = "changed after generation" in str(exc)
                finally:
                    semantic_analysis.embed_fragments = original_embed
                    semantic_analysis.cluster_embeddings = original_cluster

                persisted = AIAnalysis.query.filter_by(
                    interview_id=bypass_interview.id,
                    analysis_type="semantic_clusters",
                ).count()
                failures += check(
                    "semantic result commit fails closed when source changes during provider work",
                    raised and persisted == 0,
                    f"raised={raised} persisted={persisted}",
                )

                db.session.remove()
                db.engine.dispose()
        except Exception as exc:
            failures += check(
                "semantic source provenance smoke",
                False,
                f"{type(exc).__name__}: {exc}",
            )
        finally:
            if app is not None:
                try:
                    from models import db
                    with app.app_context():
                        db.session.remove()
                        db.engine.dispose()
                except Exception:
                    pass
            config.DATABASE_URI = original["DATABASE_URI"]
            config.UPLOAD_DIR = original["UPLOAD_DIR"]
            config.OUTPUT_DIR = original["OUTPUT_DIR"]

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
