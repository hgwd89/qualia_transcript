from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"expected one match in {path}, found {count}")
    p.write_text(text.replace(old, new, 1), encoding="utf-8")


path = "scripts/audit_production_readiness_v2.py"
replace_once(
    path,
    '''import argparse\nimport json\nimport sqlite3\nimport sys\n''',
    '''import argparse\nimport json\nimport os\nimport sqlite3\nimport sys\n''',
)
replace_once(
    path,
    '''from services.readiness_validation import (\n    load_raw_text_snapshots,\n    validate_generated_artifact,\n    validate_latest_backup,\n)\n''',
    '''from services.readiness_validation import (\n    load_raw_text_snapshots,\n    validate_generated_artifact,\n    validate_latest_backup,\n)\nfrom services.secret_store import (\n    PROTECTED_PREFIX,\n    SECRET_SETTING_KEYS,\n    SecretStorageError,\n    unprotect_secret,\n)\n''',
)
needle = '''        if "processing_jobs" not in tables:\n'''
insert = '''        if "app_settings" in tables:\n            secret_keys = sorted(SECRET_SETTING_KEYS)\n            placeholders = ",".join("?" for _ in secret_keys)\n            secret_rows = con.execute(\n                f"SELECT key, value FROM app_settings WHERE key IN ({placeholders}) ORDER BY key",\n                secret_keys,\n            ).fetchall()\n            plaintext_keys: list[str] = []\n            unreadable_keys: list[dict] = []\n            protected_count = 0\n            for row in secret_rows:\n                key = str(row["key"] or "")\n                value = str(row["value"] or "")\n                if not value:\n                    continue\n                if not value.startswith(PROTECTED_PREFIX):\n                    plaintext_keys.append(key)\n                    continue\n                protected_count += 1\n                if os.name == "nt":\n                    try:\n                        unprotect_secret(value)\n                    except SecretStorageError as exc:\n                        unreadable_keys.append({\n                            "key": key,\n                            "error": f"{type(exc).__name__}: {exc}",\n                        })\n            info["protected_secret_setting_count"] = protected_count\n            if plaintext_keys:\n                _issue(\n                    blockers,\n                    "secret_settings_plaintext",\n                    "Sensitive API settings remain stored as plaintext",\n                    keys=plaintext_keys,\n                    count=len(plaintext_keys),\n                )\n            if unreadable_keys:\n                _issue(\n                    blockers,\n                    "secret_settings_unreadable",\n                    "DPAPI-protected settings cannot be decrypted by the current Windows user",\n                    settings=unreadable_keys,\n                    count=len(unreadable_keys),\n                )\n\n        if "processing_jobs" not in tables:\n'''
replace_once(path, needle, insert)
print("readiness secret-storage patch applied")
