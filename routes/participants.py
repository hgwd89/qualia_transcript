import re

from flask import Blueprint, render_template, request, redirect, url_for, flash
from sqlalchemy.exc import IntegrityError

from models import db
from models.project import Project
from models.participant import Participant, ParticipantAttribute
from models.interview import Interview
from models.segment import Segment
from models.speaker_assignment import SpeakerAssignment

bp = Blueprint("participants", __name__)
_PARTICIPANT_AUTO_CODE_RE = re.compile(r"^P(\d+)$")


def _get_project_participant_or_404(project_id: int, participant_id: int) -> Participant:
    return (
        Participant.query
        .filter_by(id=participant_id, project_id=project_id)
        .first_or_404()
    )


def _participant_usage_counts(participant_id: int) -> dict[str, int]:
    return {
        "interviews": Interview.query.filter_by(participant_id=participant_id).count(),
        "segments": Segment.query.filter_by(participant_id=participant_id).count(),
        "speaker_assignments": SpeakerAssignment.query.filter_by(participant_id=participant_id).count(),
    }


def _next_participant_code(project_id: int) -> str:
    """Return a monotonic Pxx code without reusing a deleted participant number."""
    highest = 0
    rows = Participant.query.filter_by(project_id=project_id).with_entities(
        Participant.participant_code
    ).all()
    for (raw_code,) in rows:
        match = _PARTICIPANT_AUTO_CODE_RE.fullmatch(str(raw_code or "").strip())
        if match:
            highest = max(highest, int(match.group(1)))
    return f"P{highest + 1:02d}"


def _participant_code_conflict(
    project_id: int,
    participant_code: str,
    *,
    exclude_participant_id: int | None = None,
) -> bool:
    query = Participant.query.filter_by(
        project_id=int(project_id),
        participant_code=str(participant_code),
    )
    if exclude_participant_id is not None:
        query = query.filter(Participant.id != int(exclude_participant_id))
    return query.first() is not None


def _duplicate_code_response(project, *, participant=None):
    flash("同じプロジェクト内で参加者コードは重複できません", "error")
    if participant is None:
        return render_template("participants/new.html", project=project), 409
    return render_template(
        "participants/edit.html",
        project=project,
        participant=participant,
    ), 409


@bp.route("/projects/<int:project_id>/participants")
def index(project_id):
    project = Project.query.get_or_404(project_id)
    return render_template("participants/index.html", project=project,
                           participants=project.participants)


@bp.route("/projects/<int:project_id>/participants/new", methods=["GET", "POST"])
def new(project_id):
    project = Project.query.get_or_404(project_id)
    if request.method == "POST":
        requested_code = request.form.get("participant_code", "").strip()
        code = requested_code or _next_participant_code(project_id)
        if _participant_code_conflict(project_id, code):
            return _duplicate_code_response(project)

        p = Participant(
            project_id=project_id,
            participant_code=code,
            display_name=request.form.get("display_name", "").strip() or None,
        )
        try:
            db.session.add(p)
            db.session.flush()

            keys = request.form.getlist("attr_key")
            values = request.form.getlist("attr_value")
            for i, (k, v) in enumerate(zip(keys, values)):
                k = k.strip()
                if k:
                    db.session.add(ParticipantAttribute(
                        participant_id=p.id,
                        attribute_key=k,
                        attribute_value=v.strip(),
                        display_order=i,
                    ))
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            return _duplicate_code_response(project)

        flash(f"参加者 {p.participant_code} を追加しました", "success")
        return redirect(url_for("participants.index", project_id=project_id))
    return render_template("participants/new.html", project=project)


@bp.route("/projects/<int:project_id>/participants/<int:participant_id>/edit",
          methods=["GET", "POST"])
def edit(project_id, participant_id):
    project = Project.query.get_or_404(project_id)
    participant = _get_project_participant_or_404(project_id, participant_id)
    if request.method == "POST":
        requested_code = request.form.get("participant_code", "").strip()
        proposed_code = requested_code or participant.participant_code
        if _participant_code_conflict(
            project_id,
            proposed_code,
            exclude_participant_id=participant.id,
        ):
            return _duplicate_code_response(project, participant=participant)

        participant.participant_code = proposed_code
        participant.display_name = request.form.get("display_name", "").strip() or None

        try:
            ParticipantAttribute.query.filter_by(participant_id=participant_id).delete()
            keys = request.form.getlist("attr_key")
            values = request.form.getlist("attr_value")
            for i, (k, v) in enumerate(zip(keys, values)):
                k = k.strip()
                if k:
                    db.session.add(ParticipantAttribute(
                        participant_id=participant_id,
                        attribute_key=k,
                        attribute_value=v.strip(),
                        display_order=i,
                    ))
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            participant = _get_project_participant_or_404(project_id, participant_id)
            return _duplicate_code_response(project, participant=participant)

        flash("参加者情報を更新しました", "success")
        return redirect(url_for("participants.index", project_id=project_id))
    return render_template("participants/edit.html", project=project, participant=participant)


@bp.route("/projects/<int:project_id>/participants/<int:participant_id>/delete",
          methods=["POST"])
def delete(project_id, participant_id):
    Project.query.get_or_404(project_id)
    participant = _get_project_participant_or_404(project_id, participant_id)
    usage = _participant_usage_counts(participant.id)
    if any(usage.values()):
        flash(
            "この参加者はインタビュー／発言／話者割当に使用されているため削除できません",
            "error",
        )
        return redirect(url_for("participants.index", project_id=project_id))

    db.session.delete(participant)
    db.session.commit()
    flash("参加者を削除しました", "info")
    return redirect(url_for("participants.index", project_id=project_id))
