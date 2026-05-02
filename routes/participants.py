from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from models import db
from models.project import Project
from models.participant import Participant, ParticipantAttribute

bp = Blueprint("participants", __name__)


@bp.route("/projects/<int:project_id>/participants")
def index(project_id):
    project = Project.query.get_or_404(project_id)
    return render_template("participants/index.html", project=project,
                           participants=project.participants)


@bp.route("/projects/<int:project_id>/participants/new", methods=["GET", "POST"])
def new(project_id):
    project = Project.query.get_or_404(project_id)
    if request.method == "POST":
        # 連番コード自動採番
        count = Participant.query.filter_by(project_id=project_id).count()
        code  = f"P{count + 1:02d}"
        p = Participant(
            project_id=project_id,
            participant_code=request.form.get("participant_code", code).strip() or code,
            display_name=request.form.get("display_name", "").strip() or None,
        )
        db.session.add(p)
        db.session.flush()  # p.id を取得

        # 属性（key=value 形式で複数）
        keys   = request.form.getlist("attr_key")
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
        flash(f"参加者 {p.participant_code} を追加しました", "success")
        return redirect(url_for("participants.index", project_id=project_id))
    return render_template("participants/new.html", project=project)


@bp.route("/projects/<int:project_id>/participants/<int:participant_id>/edit",
          methods=["GET", "POST"])
def edit(project_id, participant_id):
    project     = Project.query.get_or_404(project_id)
    participant = Participant.query.get_or_404(participant_id)
    if request.method == "POST":
        participant.participant_code = request.form.get("participant_code", "").strip() or participant.participant_code
        participant.display_name     = request.form.get("display_name", "").strip() or None

        # 既存属性を削除してから再登録
        ParticipantAttribute.query.filter_by(participant_id=participant_id).delete()
        keys   = request.form.getlist("attr_key")
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
        flash("参加者情報を更新しました", "success")
        return redirect(url_for("participants.index", project_id=project_id))
    return render_template("participants/edit.html", project=project, participant=participant)


@bp.route("/projects/<int:project_id>/participants/<int:participant_id>/delete",
          methods=["POST"])
def delete(project_id, participant_id):
    participant = Participant.query.get_or_404(participant_id)
    db.session.delete(participant)
    db.session.commit()
    flash("参加者を削除しました", "info")
    return redirect(url_for("participants.index", project_id=project_id))
