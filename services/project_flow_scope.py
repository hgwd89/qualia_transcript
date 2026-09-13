from __future__ import annotations

from dataclasses import dataclass

from models.interview_flow import InterviewFlow


ANALYSIS_READY_INTERVIEW_STATUSES = {"mapped", "analyzed", "done"}


class ProjectFlowScopeError(ValueError):
    """The project does not provide one safe, unambiguous flow scope."""

    def __init__(self, code: str, message: str, *, interview_ids: list[int] | None = None):
        self.code = str(code)
        self.interview_ids = [int(value) for value in (interview_ids or [])]
        super().__init__(message)


@dataclass(frozen=True)
class IntegratedAnalysisScope:
    flow: InterviewFlow
    interview_ids: tuple[int, ...]


def resolve_only_configured_flow(project) -> InterviewFlow:
    """Return the only configured flow; never guess when multiple flows exist."""
    flows = list(project.interview_flows)
    if not flows:
        raise ProjectFlowScopeError(
            "missing_project_flow",
            "インタビューフローが登録されていません",
        )
    if len(flows) != 1:
        raise ProjectFlowScopeError(
            "ambiguous_flow_assignment",
            "複数のインタビューフローがあるため、未設定インタビューへ自動でフローを割り当てられません",
        )
    return flows[0]


def resolve_pipeline_interview_flow(project, interview) -> tuple[InterviewFlow, bool]:
    """Return the interview flow and whether a safe single-flow auto-assignment is needed."""
    if interview.flow_id is None:
        return resolve_only_configured_flow(project), True

    flow_id = int(interview.flow_id)
    flow = next(
        (candidate for candidate in project.interview_flows if int(candidate.id) == flow_id),
        None,
    )
    if flow is None:
        raise ProjectFlowScopeError(
            "interview_flow_project_mismatch",
            "インタビューがこのプロジェクトに属さないフローを参照しています",
            interview_ids=[int(interview.id)],
        )
    return flow, False


def resolve_integrated_analysis_scope(project) -> IntegratedAnalysisScope:
    """Resolve the one supported source flow for project integrated analysis.

    The current integrated artifact persists exactly one `source_flow_id`. It must
    therefore never guess among multiple configured flow versions. Every
    participant interview must also be mapped-or-later and explicitly belong to
    that sole flow, preventing silent participant omission before provider work.
    """
    try:
        flow = resolve_only_configured_flow(project)
    except ProjectFlowScopeError as exc:
        if exc.code == "ambiguous_flow_assignment":
            raise ProjectFlowScopeError(
                "integrated_multiple_configured_flows",
                "複数のインタビューフローがあるため、現在の単一フロー統合分析は実行できません",
            ) from exc
        raise

    target_interviews = [
        interview
        for interview in project.interviews
        if interview.participant_id is not None
    ]
    if not target_interviews:
        raise ProjectFlowScopeError(
            "no_participant_interviews",
            "統合分析の対象となる参加者付きインタビューがありません",
        )

    missing_flow_ids = [
        int(interview.id)
        for interview in target_interviews
        if interview.flow_id is None
    ]
    if missing_flow_ids:
        raise ProjectFlowScopeError(
            "integrated_interview_missing_flow",
            "フロー未設定のインタビューがあるため統合分析できません",
            interview_ids=missing_flow_ids,
        )

    wrong_flow_ids = [
        int(interview.id)
        for interview in target_interviews
        if int(interview.flow_id) != int(flow.id)
    ]
    if wrong_flow_ids:
        raise ProjectFlowScopeError(
            "integrated_interview_flow_mismatch",
            "統合分析対象のインタビューが同一フローに揃っていません",
            interview_ids=wrong_flow_ids,
        )

    incomplete_ids = [
        int(interview.id)
        for interview in target_interviews
        if str(interview.status or "") not in ANALYSIS_READY_INTERVIEW_STATUSES
    ]
    if incomplete_ids:
        raise ProjectFlowScopeError(
            "integrated_interview_not_mapped",
            "全対象インタビューのマッピング完了後に統合分析を実行してください",
            interview_ids=incomplete_ids,
        )

    return IntegratedAnalysisScope(
        flow=flow,
        interview_ids=tuple(sorted(int(interview.id) for interview in target_interviews)),
    )
