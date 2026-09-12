import os
import sys
import tempfile
from pathlib import Path


def check(name: str, ok: bool, detail: str = "") -> int:
    status = "PASS" if ok else "FAIL"
    suffix = f": {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return 0 if ok else 1


def main() -> int:
    if os.name != "nt":
        print("[PASS] DPAPI secret-store smoke skipped on non-Windows")
        return 0

    failures = 0
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    import config
    original_uri = config.DATABASE_URI

    with tempfile.TemporaryDirectory(prefix="qualia_secret_store_") as tmp:
        root = Path(tmp)
        config.DATABASE_URI = f"sqlite:///{(root / 'secrets.db').as_posix()}"
        try:
            from app import create_app
            from models import db
            from models.setting import AppSetting
            from services.secret_store import (
                PROTECTED_PREFIX,
                SecretStorageError,
                get_secret_setting,
                is_protected_secret,
                migrate_legacy_plaintext_secrets,
                protect_secret,
                set_secret_setting,
                unprotect_secret,
            )

            app = create_app()
            app.config["TESTING"] = True

            with app.app_context():
                sample = "qt-test-secret-123_日本語"
                protected = protect_secret(sample)
                failures += check(
                    "DPAPI protected value is opaque",
                    protected.startswith(PROTECTED_PREFIX)
                    and sample not in protected,
                )
                failures += check(
                    "DPAPI round trip restores UTF-8 secret",
                    unprotect_secret(protected) == sample,
                )

                AppSetting.set("openai_api_key", "legacy-plaintext-openai-key")
                migration = migrate_legacy_plaintext_secrets()
                raw_after_migration = AppSetting.get("openai_api_key", "")
                failures += check(
                    "legacy plaintext secret is migrated",
                    "openai_api_key" in migration.migrated_keys
                    and is_protected_secret(raw_after_migration)
                    and "legacy-plaintext-openai-key" not in raw_after_migration,
                )
                failures += check(
                    "migrated secret remains readable",
                    get_secret_setting("openai_api_key") == "legacy-plaintext-openai-key",
                )

                set_secret_setting("rakuten_access_key", "rakuten-secret-value")
                raw_rakuten = AppSetting.get("rakuten_access_key", "")
                failures += check(
                    "new secret is stored only as DPAPI ciphertext",
                    is_protected_secret(raw_rakuten)
                    and "rakuten-secret-value" not in raw_rakuten
                    and get_secret_setting("rakuten_access_key") == "rakuten-secret-value",
                )

                client = app.test_client()
                response = client.get("/settings")
                html = response.get_data(as_text=True)
                failures += check(
                    "settings page does not expose decrypted secret",
                    response.status_code == 200
                    and "legacy-plaintext-openai-key" not in html
                    and "rakuten-secret-value" not in html,
                )

                response = client.post(
                    "/settings",
                    data={"openai_api_key": "replacement-openai-secret"},
                    follow_redirects=False,
                )
                stored_replacement = AppSetting.get("openai_api_key", "")
                failures += check(
                    "settings POST stores password field with DPAPI",
                    response.status_code == 302
                    and is_protected_secret(stored_replacement)
                    and "replacement-openai-secret" not in stored_replacement
                    and get_secret_setting("openai_api_key") == "replacement-openai-secret",
                )

                AppSetting.set("whisper_model", "baseline-whisper")
                from routes import settings as settings_route

                original_route_set_secret = settings_route.set_secret_setting

                def fail_last_secret(key, value, *, commit=True):
                    if key == "rakuten_access_key":
                        raise SecretStorageError("simulated DPAPI failure")
                    return original_route_set_secret(key, value, commit=commit)

                settings_route.set_secret_setting = fail_last_secret
                try:
                    failed_response = client.post(
                        "/settings",
                        data={
                            "openai_api_key": "must-not-partially-save",
                            "whisper_model": "must-not-partially-save-model",
                            "rakuten_access_key": "trigger-failure",
                        },
                        follow_redirects=False,
                    )
                finally:
                    settings_route.set_secret_setting = original_route_set_secret

                failures += check(
                    "multi-field settings failure rolls back every earlier field",
                    failed_response.status_code == 302
                    and get_secret_setting("openai_api_key") == "replacement-openai-secret"
                    and AppSetting.get("whisper_model") == "baseline-whisper"
                    and get_secret_setting("rakuten_access_key") == "rakuten-secret-value",
                )

                from services import semantic_analysis

                captured: dict[str, str | None] = {}
                original_openai = semantic_analysis.openai.OpenAI

                class CapturingOpenAI:
                    def __init__(self, api_key=None, **_kwargs):
                        captured["api_key"] = api_key

                semantic_analysis.openai.OpenAI = CapturingOpenAI
                try:
                    semantic_analysis._client()
                finally:
                    semantic_analysis.openai.OpenAI = original_openai

                failures += check(
                    "semantic embeddings receive decrypted OpenAI key",
                    captured.get("api_key") == "replacement-openai-secret"
                    and captured.get("api_key") != stored_replacement,
                )

                db.session.remove()
                db.engine.dispose()
        finally:
            config.DATABASE_URI = original_uri

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
