import argparse
import json
import os
import sys

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from app import create_app
from services.semantic_analysis import run_semantic_cluster_analysis


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Semantic cluster analysis CLI")
    parser.add_argument("--interview-id", type=int, required=True, help="target interview id")
    parser.add_argument("--max-segments", type=int, default=None, help="max respondent segments after filtering")
    parser.add_argument("--no-ai", action="store_true", help="skip AI cluster summarization")

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="do not save AIAnalysis (default)")
    mode.add_argument("--save", action="store_true", help="save AIAnalysis with analysis_type=semantic_clusters")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    save = bool(args.save)
    if not args.save and not args.dry_run:
        save = False  # default dry-run

    app = create_app()
    with app.app_context():
        try:
            result = run_semantic_cluster_analysis(
                interview_id=args.interview_id,
                save=save,
                max_segments=args.max_segments,
                no_ai=bool(args.no_ai),
            )
        except Exception as e:
            print(json.dumps({
                "ok": False,
                "error_type": type(e).__name__,
                "error_message": str(e),
            }, ensure_ascii=False))
            return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("ok", False):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
