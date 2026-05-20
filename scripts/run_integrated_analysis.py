import argparse
import json
import os
import sys

from flask import Flask

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import config
from models import db
from services.integrated_analysis import run_integrated_interview_analysis


def create_analysis_app() -> Flask:
    """Create a minimal app context for DB-backed analysis without importing route modules.

    Do not import app.create_app here: app.py imports transcription routes, which import
    faster_whisper/transformers. This CLI must remain no-Whisper and no-external-API by default.
    """
    app = Flask(__name__)
    app.config["SECRET_KEY"] = config.SECRET_KEY
    app.config["SQLALCHEMY_DATABASE_URI"] = config.DATABASE_URI
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    return app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Integrated interview analysis CLI (no-ai dry-run)")
    parser.add_argument("--interview-id", type=int, required=True, help="target interview id")
    parser.add_argument("--dry-run", action="store_true", help="run without saving (default)")
    parser.add_argument("--save", action="store_true", help="not supported yet")
    parser.add_argument("--no-ai", action="store_true", help="no-ai mode (default true)")
    parser.add_argument("--max-quotes", type=int, default=20, help="max supporting quotes in payload")
    parser.add_argument("--include-needs-review", action="store_true", help="include needs_review segments")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    # Current phase: no-ai dry-run only.
    no_ai = True
    save = bool(args.save)

    app = create_analysis_app()
    with app.app_context():
        try:
            result = run_integrated_interview_analysis(
                interview_id=args.interview_id,
                no_ai=no_ai,
                save=save,
                max_quotes=args.max_quotes,
                include_needs_review=bool(args.include_needs_review),
            )
        except Exception as e:
            print(json.dumps({
                "ok": False,
                "error_type": type(e).__name__,
                "error_message": str(e),
            }, ensure_ascii=False, indent=2))
            return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
