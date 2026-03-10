"""
Stage 2: Design
Responsibility: Architecture & System Design
Output: design.md, api_spec.json
"""

from typing import Any, Dict
from .base import Stage


class DesignStage(Stage):
    def __init__(self):
        super().__init__(name="Design", description="Design system architecture and API.")

    def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        self.log("Starting system design...")
        
        if "analysis" not in context:
            self.log("No analysis found in context. Cannot proceed.", level="error")
            context["status"] = "failed"
            context["error"] = "Missing analysis step"
            return context

        # TODO: Call LLM to design architecture
        # 1. Define modules/components
        # 2. Define data flow
        # 3. Define interfaces
        
        design_result = {
            "modules": [],  # TODO: Define modules
            "data_flow": [],  # TODO: Define flow
            "interfaces": []  # TODO: Define interfaces
        }
        
        context["artifacts"]["design.md"] = self._generate_design_md(design_result)
        context["design"] = design_result
        self.log("System design completed.")
        return context

    def _generate_design_md(self, data: Dict) -> str:
        # TODO: Use LLM to generate structured markdown
        return "# System Design\n\n## TODO\n- Define modules\n- Define data flow\n- Define interfaces"
