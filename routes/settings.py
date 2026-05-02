from flask import Blueprint, render_template, request, redirect, url_for, flash
import config
from models.setting import AppSetting

bp = Blueprint("settings", __name__)

SETTING_KEYS = [
    ("anthropic_api_key", "Anthropic APIキー", "password"),
    ("whisper_model",     "Whisperモデル",      "text"),
]


@bp.route("/settings", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        for key, _, _ in SETTING_KEYS:
            val = request.form.get(key, "").strip()
            if val:
                AppSetting.set(key, val)
        flash("設定を保存しました", "success")
        return redirect(url_for("settings.index"))

    values = {key: AppSetting.get(key, "") for key, _, _ in SETTING_KEYS}
    return render_template("settings/index.html",
                           setting_keys=SETTING_KEYS,
                           values=values,
                           service_name=config.SERVICE_NAME)
