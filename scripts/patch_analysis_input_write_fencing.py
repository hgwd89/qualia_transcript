from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def replace_exact(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"expected patch target not found in {path}: {old[:160]!r}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def replace_block(path: Path, start: str, end: str, replacement: str) -> None:
    text = path.read_text(encoding="utf-8")
    start_index = text.index(start)
    end_index = text.index(end, start_index)
    path.write_text(text[:start_index] + replacement + text[end_index:], encoding="utf-8")


def patch_routes() -> None:
    path = ROOT / "routes" / "interviews.py"
    replace_exact(
        path,
        "from services.upload_manager import save_and_register_media\n",
        "from services.upload_manager import save_and_register_media\n"
        "from services.research_input_guard import (\n"
        "    ResearchInputWriteBlocked,\n"
        "    begin_interview_input_write,\n"
        ")\n",
    )
    replace_exact(
        path,
        'SPEAKER_ROLES = ("moderator", "respondent", "observer", "unknown")\n\n\n',
        '''SPEAKER_ROLES = ("moderator", "respondent", "observer", "unknown")\n\n\ndef _input_write_blocked_response(exc: ResearchInputWriteBlocked):\n    return jsonify({\n        "ok": False,\n        "error": "処理中のジョブが分析入力を使用しているため、完了または失敗後に変更してください。",\n        "active_job_ids": list(exc.active_job_ids),\n    }), 409\n\n\n''',
    )

    mapping_start = '''@bp.route("/api/interviews/<int:interview_id>/segments/<int:segment_id>/mapping", methods=["POST"])\ndef upsert_segment_mapping(interview_id, segment_id):\n'''
    mapping_end = '''\n\n@bp.route("/api/segments/<int:segment_id>/flags", methods=["POST"])\n'''
    mapping_replacement = '''@bp.route("/api/interviews/<int:interview_id>/segments/<int:segment_id>/mapping", methods=["POST"])\ndef upsert_segment_mapping(interview_id, segment_id):\n    interview = Interview.query.get_or_404(interview_id)\n    seg = Segment.query.get_or_404(segment_id)\n    if seg.interview_id != interview.id:\n        return jsonify({"ok": False, "error": "segment does not belong to interview"}), 400\n\n    data = request.get_json(force=True, silent=True) or {}\n    raw_question_id = data.get("question_id")\n    notes = (data.get("notes") or "").strip() or None\n    mapped_by = "human"\n\n    question_id = None\n    if raw_question_id not in (None, "", "null"):\n        try:\n            question_id = int(raw_question_id)\n        except (TypeError, ValueError):\n            return jsonify({"ok": False, "error": "invalid question_id"}), 400\n\n    try:\n        interview = begin_interview_input_write(interview.id)\n    except ResearchInputWriteBlocked as exc:\n        return _input_write_blocked_response(exc)\n    except ValueError:\n        return jsonify({"ok": False, "error": "interview not found"}), 404\n\n    seg = db.session.get(Segment, int(segment_id))\n    if seg is None:\n        db.session.rollback()\n        return jsonify({"ok": False, "error": "segment not found"}), 404\n    if int(seg.interview_id) != int(interview.id):\n        db.session.rollback()\n        return jsonify({"ok": False, "error": "segment does not belong to interview"}), 400\n\n    if question_id is not None:\n        if not interview.flow_id:\n            db.session.rollback()\n            return jsonify({"ok": False, "error": "interview flow is not set"}), 400\n\n        question = (\n            InterviewFlowQuestion.query\n            .join(InterviewFlowQuestion.section)\n            .filter(InterviewFlowQuestion.id == question_id)\n            .filter_by(flow_id=interview.flow_id)\n            .first()\n        )\n        if not question:\n            db.session.rollback()\n            return jsonify({"ok": False, "error": "question does not belong to interview flow"}), 400\n\n    mappings = (\n        UtteranceMapping.query\n        .filter_by(segment_id=seg.id)\n        .order_by(UtteranceMapping.id.asc())\n        .all()\n    )\n    primary = mappings[0] if mappings else None\n    if primary is None:\n        primary = UtteranceMapping(segment_id=seg.id)\n        db.session.add(primary)\n        mappings = [primary]\n\n    for extra in mappings[1:]:\n        db.session.delete(extra)\n\n    primary.question_id = question_id\n    primary.mapped_by = mapped_by\n    primary.confidence = 1.0\n    primary.is_unclassified = question_id is None\n    primary.notes = notes or ("manual_unclassified" if question_id is None else "manual_assign")\n\n    db.session.commit()\n    return jsonify({\n        "ok": True,\n        "segment_id": seg.id,\n        "mapping_id": primary.id,\n        "question_id": primary.question_id,\n        "is_unclassified": primary.is_unclassified,\n        "mapped_by": primary.mapped_by,\n        "confidence": primary.confidence,\n    })\n'''
    replace_block(path, mapping_start, mapping_end, mapping_replacement)

    role_start = '''@bp.route("/interviews/<int:interview_id>/segments/<int:segment_id>/role", methods=["POST"])\ndef update_segment_role(interview_id, segment_id):\n'''
    role_end = '''\n\n@bp.route("/api/interviews/<int:interview_id>/segments/<int:segment_id>/product-hint")\n'''
    role_replacement = '''@bp.route("/interviews/<int:interview_id>/segments/<int:segment_id>/role", methods=["POST"])\ndef update_segment_role(interview_id, segment_id):\n    interview = Interview.query.get_or_404(interview_id)\n    seg = Segment.query.get_or_404(segment_id)\n    if seg.interview_id != interview.id:\n        return jsonify({"ok": False, "error": "segment does not belong to interview"}), 400\n\n    data = request.get_json(silent=True)\n    if not isinstance(data, dict) or not data:\n        return jsonify({"ok": False, "error": "valid JSON object is required"}), 400\n    if "speaker_role" not in data and "participant_id" not in data:\n        return jsonify({"ok": False, "error": "no role fields supplied"}), 400\n\n    requested_speaker_role = None\n    if "speaker_role" in data:\n        requested_speaker_role = str(data.get("speaker_role") or "").strip()\n        if requested_speaker_role not in SPEAKER_ROLES:\n            return jsonify({"ok": False, "error": "invalid speaker_role"}), 400\n\n    participant_supplied = "participant_id" in data\n    requested_participant_id = data.get("participant_id") if participant_supplied else None\n    if participant_supplied and requested_participant_id not in (None, ""):\n        try:\n            requested_participant_id = int(requested_participant_id)\n        except (TypeError, ValueError):\n            return jsonify({"ok": False, "error": "invalid participant_id"}), 400\n    elif participant_supplied:\n        requested_participant_id = None\n\n    try:\n        interview = begin_interview_input_write(interview.id)\n    except ResearchInputWriteBlocked as exc:\n        return _input_write_blocked_response(exc)\n    except ValueError:\n        return jsonify({"ok": False, "error": "interview not found"}), 404\n\n    seg = db.session.get(Segment, int(segment_id))\n    if seg is None:\n        db.session.rollback()\n        return jsonify({"ok": False, "error": "segment not found"}), 404\n    if int(seg.interview_id) != int(interview.id):\n        db.session.rollback()\n        return jsonify({"ok": False, "error": "segment does not belong to interview"}), 400\n\n    speaker_role = seg.speaker_role or "unknown"\n    if requested_speaker_role is not None:\n        speaker_role = requested_speaker_role\n\n    participant_id = seg.participant_id\n    if participant_supplied:\n        participant_id = requested_participant_id\n        if participant_id is not None:\n            participant = db.session.get(Participant, int(participant_id))\n            if not participant or int(participant.project_id) != int(interview.project_id):\n                db.session.rollback()\n                return jsonify({"ok": False, "error": "participant does not belong to project"}), 400\n\n    seg.speaker_role = speaker_role\n    seg.participant_id = participant_id\n    db.session.commit()\n    return jsonify({\n        "ok": True,\n        "segment_id": seg.id,\n        "speaker_role": seg.speaker_role,\n        "participant_id": seg.participant_id,\n    })\n'''
    replace_block(path, role_start, role_end, role_replacement)


def patch_workflow() -> None:
    path = ROOT / ".github" / "workflows" / "processing-jobs.yml"
    replace_exact(
        path,
        '''      - name: Run role normalization regression\n        run: python tests/smoke_role_normalization.py\n''',
        '''      - name: Run processing input fencing regression\n        run: python tests/smoke_processing_input_fencing.py\n\n      - name: Run role normalization regression\n        run: python tests/smoke_role_normalization.py\n''',
    )


def main() -> None:
    patch_routes()
    patch_workflow()
    print("analysis input write fencing patch applied")


if __name__ == "__main__":
    main()
