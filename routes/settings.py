from flask import Blueprint, render_template, request, redirect, url_for, flash
import config
from models.setting import AppSetting
from services.secret_store import (
    SECRET_SETTING_KEYS,
    SecretStorageError,
    secret_setting_is_configured,
    set_secret_setting,
)

bp = Blueprint("settings", __name__)

SETTING_KEYS = [
    ("openai_api_key", "OpenAI APIキー", "password"),
    ("whisper_model",     "Whisperモデル",      "text"),
    ("product_hint_provider", "商品照合プロバイダ (rakuten/none)", "text"),
    ("rakuten_application_id", "楽天API applicationId", "password"),
    ("rakuten_access_key", "楽天API accessKey (任意)", "password"),
    ("rakuten_affiliate_id", "楽天 affiliateId (任意)", "text"),
]

SECRET_FALLBACKS = {
    "openai_api_key": config.OPENAI_API_KEY,
    "rakuten_application_id": config.RAKUTEN_APPLICATION_ID,
    "rakuten_access_key": config.RAKUTEN_ACCESS_KEY,
}


@bp.route("/settings", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        try:
            for key, _, _ in SETTING_KEYS:
                val = request.form.get(key, "").strip()
                if not val:
                    continue
                if key in SECRET_SETTING_KEYS:
                    set_secret_setting(key, val)
                else:
                    AppSetting.set(key, val)
        except SecretStorageError as exc:
            flash(f"秘密設定を安全に保存できませんでした: {exc}", "error")
            return redirect(url_for("settings.index"))
        flash("設定を保存しました", "success")
        return redirect(url_for("settings.index"))

    values = {}
    for key, _, input_type in SETTING_KEYS:
        if input_type == "password":
            values[key] = secret_setting_is_configured(
                key,
                SECRET_FALLBACKS.get(key, ""),
            )
        else:
            values[key] = AppSetting.get(key, "")

    return render_template("settings/index.html",
                           setting_keys=SETTING_KEYS,
                           values=values,
                           service_name=config.SERVICE_NAME)
