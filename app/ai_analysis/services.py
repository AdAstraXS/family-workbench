class AiAnalysisService:
    """AI 调用统一入口。第一阶段先保留接口，后续再接入具体服务商。"""

    def analyze(self, analysis_request):
        raise NotImplementedError("AI API integration will be implemented in a later phase.")
