"""
Stage 1: Analyze
Responsibility: Requirement Analysis
Output: requirements.md
"""

from typing import Any, Dict
from .base import Stage


class AnalyzeStage(Stage):
    def __init__(self):
        super().__init__(name="Analyze", description="Analyze user requirements and define scope.")

    def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        self.log("Starting requirement analysis...")
        prompt = context.get("prompt", "")
        
        if not prompt:
            self.log("No prompt provided in context.", level="error")
            context["status"] = "failed"
            context["error"] = "Missing prompt"
            return context

        # TODO: Call LLM to analyze requirements
        # 1. Extract key features
        # 2. Identify constraints
        # 3. Define success criteria
        
        analysis_result = {
            "raw_prompt": prompt,
            "features": [],  # TODO: Extract features
            "constraints": [],  # TODO: Extract constraints
            "success_criteria": []  # TODO: Define criteria
        }
        
        context["artifacts"]["requirements.md"] = self._generate_requirements_md(analysis_result)
        context["analysis"] = analysis_result
        self.log("Requirement analysis completed.")
        return context

    def _generate_requirements_md(self, data: Dict) -> str:
        # TODO: Use LLM to generate structured markdown
        return f"# Requirements Analysis\n\n**Prompt**: {data['raw_prompt']}\n\n## TODO\n- Analyze features\n- Define constraints\n- Set success criteria"
