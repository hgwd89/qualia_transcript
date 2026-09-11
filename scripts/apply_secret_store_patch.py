from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"expected one match in {path}, found {count}")
    p.write_text(text.replace(old, new, 1), encoding="utf-8")


replace_once(
    "services/transcription.py",
    '''from models.setting import AppSetting\nfrom services.upload_manager import get_media_full_path\n''',
    '''from services.secret_store import get_secret_setting\nfrom services.upload_manager import get_media_full_path\n''',
)
replace_once(
    "services/transcription.py",
    '''def _openai_client() -> OpenAI:\n    api_key = AppSetting.get("openai_api_key") or config.OPENAI_API_KEY\n    return OpenAI(api_key=api_key)\n''',
    '''def _openai_client() -> OpenAI:\n    api_key = get_secret_setting("openai_api_key", config.OPENAI_API_KEY)\n    return OpenAI(api_key=api_key)\n''',
)

replace_once(
    "services/product_hint.py",
    '''from models.setting import AppSetting\nfrom services.domain_glossary import find_glossary_hints\n''',
    '''from models.setting import AppSetting\nfrom services.domain_glossary import find_glossary_hints\nfrom services.secret_store import get_secret_setting\n''',
)
replace_once(
    "services/product_hint.py",
    '''def _rakuten_app_id() -> str:\n    return _get_setting("rakuten_application_id", config.RAKUTEN_APPLICATION_ID)\n\n\ndef _rakuten_access_key() -> str:\n    return _get_setting("rakuten_access_key", config.RAKUTEN_ACCESS_KEY)\n''',
    '''def _rakuten_app_id() -> str:\n    return get_secret_setting("rakuten_application_id", config.RAKUTEN_APPLICATION_ID).strip()\n\n\ndef _rakuten_access_key() -> str:\n    return get_secret_setting("rakuten_access_key", config.RAKUTEN_ACCESS_KEY).strip()\n''',
)

replace_once(
    "app.py",
    '''    _run_migrations(app)\n\n    return app\n''',
    '''    _run_migrations(app)\n\n    # Existing installations may contain plaintext API credentials from older\n    # versions. On Windows, migrate them to current-user DPAPI storage at startup.\n    with app.app_context():\n        try:\n            from services.secret_store import migrate_legacy_plaintext_secrets\n            migrated = migrate_legacy_plaintext_secrets()\n            if migrated.migrated_keys:\n                app.logger.info(\n                    "migrated legacy plaintext secret settings to DPAPI: %s",\n                    ", ".join(migrated.migrated_keys),\n                )\n        except Exception:\n            # Preserve startup/data access if Windows credential protection fails;\n            # readiness/security checks can then surface the remaining plaintext.\n            app.logger.exception("secret setting DPAPI migration failed")\n\n    return app\n''',
)

print("secret-store integration patch applied")
