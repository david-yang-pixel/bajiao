class AnalysisUnavailable(RuntimeError):
    """Safe, user-facing diagnostic without provider bodies or credentials."""


class AnalysisBusy(RuntimeError):
    pass
