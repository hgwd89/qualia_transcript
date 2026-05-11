import subprocess
import sys
import tempfile
from pathlib import Path

from docx import Document
from openpyxl import load_workbook


FLAG_TYPES = ("favorite", "quote", "exclude", "needs_review")
MAPPED_TEXT = "Output flag smoke mapped segment text"
UNCLASSIFIED_TEXT = "Output flag smoke unclassified segment text"


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


def _path_is_under(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _cell_text(value) -> str:
    return "" if value is None else str(value)


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

        # Import related models so SQLAlchemy can resolve relationship strings.
        from models.analysis import AIAnalysis  # noqa: F401
        from models.generated_file import GeneratedFile
        from models.interview import Interview, MediaFile, Transcription  # noqa: F401
        from models.interview_flow import (
            InterviewFlow,
            InterviewFlowQuestion,
            InterviewFlowSection,
        )
        from models.participant import Participant
        from models.project import Project
        from models.quote_candidate import QuoteCandidate  # noqa: F401
        from models.review_item import ReviewItem  # noqa: F401
        from models.segment import Segment, UtteranceMapping
        from models.segment_flag import SegmentFlag
        from models.speaker_assignment import SpeakerAssignment  # noqa: F401
        from services.report_formatted import generate_formatted_sheet
        from services.report_verbatim import generate_verbatim
    except Exception as e:
        print_result("imports", False, f"{type(e).__name__}: {e}")
        return 1

    original_database_uri = config.DATABASE_URI
    original_upload_dir = config.UPLOAD_DIR
    original_output_dir = config.OUTPUT_DIR

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_root = Path(tmp_dir)
        tmp_output_dir = tmp_root / "outputs"
        config.DATABASE_URI = f"sqlite:///{(tmp_root / 'outputs_flags_smoke.db').as_posix()}"
        config.UPLOAD_DIR = str(tmp_root / "uploads")
        config.OUTPUT_DIR = str(tmp_output_dir)

        try:
            app = create_app()

            failures += 0 if print_result(
                "temporary database configured",
                config.DATABASE_URI.startswith("sqlite:///")
                and config.DATABASE_URI.endswith("/outputs_flags_smoke.db"),
                config.DATABASE_URI,
            ) else 1
            failures += 0 if print_result(
                "temporary upload/output dirs configured",
                str(tmp_root) in config.UPLOAD_DIR and str(tmp_root) in config.OUTPUT_DIR,
            ) else 1

            with app.app_context():
                project = Project(name="Output Flags Smoke")
                db.session.add(project)
                db.session.flush()

                participant = Participant(
                    project_id=project.id,
                    participant_code="P01",
                    display_name="Smoke Participant",
                )
                db.session.add(participant)
                db.session.flush()

                flow = InterviewFlow(project_id=project.id, title="Smoke Flow")
                db.session.add(flow)
                db.session.flush()

                section = InterviewFlowSection(flow_id=flow.id, title="Section 1", seq=1)
                db.session.add(section)
                db.session.flush()

                question = InterviewFlowQuestion(
                    section_id=section.id,
                    question_code="Q1",
                    question_text="Smoke question",
                    is_key_question=True,
                    seq=1,
                )
                db.session.add(question)
                db.session.flush()

                interview = Interview(
                    project_id=project.id,
                    participant_id=participant.id,
                    flow_id=flow.id,
                    status="mapped",
                )
                db.session.add(interview)
                db.session.flush()

                mapped_segment = Segment(
                    interview_id=interview.id,
                    participant_id=participant.id,
                    speaker_label="SPEAKER_00",
                    speaker_role="respondent",
                    start_sec=10.0,
                    end_sec=20.0,
                    text=MAPPED_TEXT,
                    seq=1,
                )
                unclassified_segment = Segment(
                    interview_id=interview.id,
                    participant_id=participant.id,
                    speaker_label="SPEAKER_00",
                    speaker_role="respondent",
                    start_sec=30.0,
                    end_sec=40.0,
                    text=UNCLASSIFIED_TEXT,
                    seq=2,
                )
                db.session.add_all([mapped_segment, unclassified_segment])
                db.session.flush()

                db.session.add(
                    UtteranceMapping(
                        segment_id=mapped_segment.id,
                        question_id=question.id,
                        mapped_by="human",
                        confidence=1.0,
                        confidence_level="high",
                        is_unclassified=False,
                    )
                )
                db.session.add(
                    UtteranceMapping(
                        segment_id=unclassified_segment.id,
                        question_id=None,
                        mapped_by="ai",
                        confidence=0.0,
                        confidence_level="low",
                        is_unclassified=True,
                    )
                )
                for flag_type in FLAG_TYPES:
                    db.session.add(
                        SegmentFlag(
                            segment_id=mapped_segment.id,
                            flag_type=flag_type,
                            note=f"mapped_{flag_type}",
                        )
                    )
                    db.session.add(
                        SegmentFlag(
                            segment_id=unclassified_segment.id,
                            flag_type=flag_type,
                            note=f"unclassified_{flag_type}",
                        )
                    )
                db.session.commit()

                project_id = project.id
                interview_id = interview.id
                mapped_baseline_text = mapped_segment.text
                unclassified_baseline_text = unclassified_segment.text

                failures += 0 if print_result(
                    "temporary fixture created",
                    all([project_id, interview_id, mapped_segment.id, unclassified_segment.id]),
                    f"project_id={project_id}, interview_id={interview_id}",
                ) else 1

                try:
                    gf_word = generate_verbatim(interview_id)
                    word_path = Path(config.OUTPUT_DIR) / gf_word.stored_path
                    failures += 0 if print_result(
                        "generate_verbatim",
                        word_path.is_file() and _path_is_under(word_path, tmp_output_dir),
                        str(word_path),
                    ) else 1
                except Exception as e:
                    gf_word = None
                    word_path = None
                    failures += 0 if print_result("generate_verbatim", False, f"{type(e).__name__}: {e}") else 1

                try:
                    gf_excel = generate_formatted_sheet(project_id)
                    excel_path = Path(config.OUTPUT_DIR) / gf_excel.stored_path
                    failures += 0 if print_result(
                        "generate_formatted_sheet",
                        excel_path.is_file() and _path_is_under(excel_path, tmp_output_dir),
                        str(excel_path),
                    ) else 1
                except Exception as e:
                    gf_excel = None
                    excel_path = None
                    failures += 0 if print_result(
                        "generate_formatted_sheet",
                        False,
                        f"{type(e).__name__}: {e}",
                    ) else 1

                if word_path and word_path.is_file():
                    doc = Document(str(word_path))
                    paragraph_texts = [p.text for p in doc.paragraphs]
                    marker_found = any("★引用候補" in text for text in paragraph_texts)
                    mapped_marker_found = any(
                        "★引用候補" in text and MAPPED_TEXT in text
                        for text in paragraph_texts
                    )
                    unclassified_marker_found = any(
                        "★引用候補" in text and UNCLASSIFIED_TEXT in text
                        for text in paragraph_texts
                    )
                    failures += 0 if print_result(
                        "word quote marker present",
                        marker_found,
                    ) else 1
                    failures += 0 if print_result(
                        "word quote marker linked to mapped segment",
                        mapped_marker_found,
                    ) else 1
                    failures += 0 if print_result(
                        "word quote marker linked to unclassified segment",
                        unclassified_marker_found,
                    ) else 1
                else:
                    failures += 0 if print_result("word output exists", False, str(word_path)) else 1

                if excel_path and excel_path.is_file():
                    wb = load_workbook(str(excel_path), data_only=True)
                    sheet_names = wb.sheetnames
                    failures += 0 if print_result(
                        "excel expected sheets",
                        "整形シート" in sheet_names and "未分類発言" in sheet_names,
                        ",".join(sheet_names),
                    ) else 1

                    ws_main = wb["整形シート"]
                    all_main_values = [
                        _cell_text(cell.value)
                        for row in ws_main.iter_rows()
                        for cell in row
                    ]
                    main_text_found = any(MAPPED_TEXT in value for value in all_main_values)
                    main_flag_line_found = any(
                        all(f"{flag_type}=true" in value for flag_type in FLAG_TYPES)
                        for value in all_main_values
                    )
                    failures += 0 if print_result(
                        "excel formatted sheet includes mapped text",
                        main_text_found,
                    ) else 1
                    failures += 0 if print_result(
                        "excel formatted sheet includes flag value line",
                        main_flag_line_found,
                    ) else 1

                    ws_unclassified = wb["未分類発言"]
                    headers = [_cell_text(c.value) for c in ws_unclassified[1]]
                    header_ok = all(name in headers for name in FLAG_TYPES)
                    failures += 0 if print_result(
                        "excel unclassified flag columns",
                        header_ok,
                        ",".join(headers),
                    ) else 1

                    flag_values_ok = False
                    if header_ok and "発言テキスト" in headers:
                        text_col = headers.index("発言テキスト") + 1
                        idx = {name: headers.index(name) + 1 for name in FLAG_TYPES}
                        for row_num in range(2, ws_unclassified.max_row + 1):
                            if ws_unclassified.cell(row_num, text_col).value == UNCLASSIFIED_TEXT:
                                values = {
                                    name: _cell_text(ws_unclassified.cell(row_num, col).value).strip().lower()
                                    for name, col in idx.items()
                                }
                                flag_values_ok = all(values[name] == "true" for name in FLAG_TYPES)
                                break

                    failures += 0 if print_result(
                        "excel unclassified flag values true",
                        flag_values_ok,
                    ) else 1
                else:
                    failures += 0 if print_result("excel output exists", False, str(excel_path)) else 1

                generated_files = GeneratedFile.query.filter_by(project_id=project_id).all()
                generated_types = {(gf.file_type, gf.file_format) for gf in generated_files}
                failures += 0 if print_result(
                    "generated files registered in temporary db",
                    ("verbatim", "docx") in generated_types
                    and ("formatted_sheet", "xlsx") in generated_types,
                    str(sorted(generated_types)),
                ) else 1

                final_mapped_text = Segment.query.get(mapped_segment.id).text
                final_unclassified_text = Segment.query.get(unclassified_segment.id).text
                failures += 0 if print_result(
                    "segment text unchanged",
                    final_mapped_text == mapped_baseline_text == MAPPED_TEXT
                    and final_unclassified_text == unclassified_baseline_text == UNCLASSIFIED_TEXT,
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
