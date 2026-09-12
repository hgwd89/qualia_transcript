from __future__ import annotations

from flask import jsonify, request


def register_request_guards(app) -> None:
    """Install narrow request-contract guards that must run before route mutation."""

    @app.before_request
    def guard_segment_role_update_contract():
        if request.endpoint != "interviews.update_segment_role":
            return None

        if not request.is_json:
            return jsonify({"ok": False, "error": "JSON object is required"}), 400

        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "valid JSON object is required"}), 400

        # The role editor persists role and participant as one atomic selection.
        # Requiring both fields prevents malformed/no-op payloads from falling
        # through to the legacy route defaults and silently clearing participant_id.
        required = {"speaker_role", "participant_id"}
        if not required.issubset(data):
            return jsonify({
                "ok": False,
                "error": "speaker_role and participant_id are required",
            }), 400

        return None
