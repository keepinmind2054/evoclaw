"""
Base class for all development stages in EvoClaw DevEngine.
"""

import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class Stage(ABC):
    """
    Abstract base class for a development stage.
    Each stage must implement the execute method.
    """

    def __init__(self, name: str, description: str):
        self.name = name
        self.description = description
        self.logger = logging.getLogger(f"{__name__}.{name}")

    @abstractmethod
    def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute the stage logic.
        
        Args:
            context: A dictionary containing the current state of the pipeline.
                     Shared across all stages.
        
        Returns:
            Updated context dictionary with new artifacts or status.
        """
        pass

    def log(self, message: str, level: str = "info"):
        """Log a message with the stage name prefix."""
        log_func = getattr(self.logger, level, self.logger.info)
        log_func(f"[{self.name}] {message}")

    # TODO: Integrate with EvoClaw's LLM provider for AI-assisted steps
    def call_llm(self, prompt: str, system_prompt: str = "") -> str:
        """
        Placeholder for LLM interaction.
        TODO: Connect to actual LLM service (e.g., Gemini, GPT-4).
        """
        self.log(f"LLM Call Triggered: {prompt[:50]}...")
        return "TODO: LLM Response Placeholder"
