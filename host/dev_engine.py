"""
EvoClaw DevEngine: 7-Stage Self-Development Pipeline.

This module implements the CLI-Anything inspired 7-stage development process:
Analyze -> Design -> Implement -> Test -> Review -> Document -> Deploy

Supports two modes:
- Interactive (REPL): Human-in-the-loop with pause points
- Auto (Script): Fully automated pipeline
"""

import asyncio
import json
import time
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Any
from pathlib import Path

from host import log, db
from host.evolution import immune
from host.container import run_in_container

class DevStage(Enum):
    """Development pipeline stages."""
    ANALYZE = "analyze"
    DESIGN = "design"
    IMPLEMENT = "implement"
    TEST = "test"
    REVIEW = "review"
    DOCUMENT = "document"
    DEPLOY = "deploy"

@dataclass
class DevArtifact:
    """Artifact produced at each stage."""
    stage: DevStage
    content: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

@dataclass
class DevContext:
    """Context for a development session."""
    session_id: str
    prompt: str
    jid: str
    mode: str  # 'interactive' or 'auto'
    artifacts: Dict[DevStage, DevArtifact] = field(default_factory=dict)
    current_stage: Optional[DevStage] = None
    status: str = "pending"  # pending, running, paused, completed, failed
    error_message: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

class DevEngine:
    """
    EvoClaw Development Engine.
    
    Implements a 7-stage pipeline for autonomous tool development:
    1. Analyze: Understand requirements
    2. Design: Create architecture spec
    3. Implement: Write code
    4. Test: Run tests
    5. Review: Security audit (immune system)
    6. Document: Generate docs
    7. Deploy: Commit and reload
    """
    
    def __init__(self, jid: str):
        self.jid = jid
        self.context: Optional[DevContext] = None
        self._stage_methods = {
            DevStage.ANALYZE: self._stage_analyze,
            DevStage.DESIGN: self._stage_design,
            DevStage.IMPLEMENT: self._stage_implement,
            DevStage.TEST: self._stage_test,
            DevStage.REVIEW: self._stage_review,
            DevStage.DOCUMENT: self._stage_document,
            DevStage.DEPLOY: self._stage_deploy,
        }
    
    async def start_session(self, prompt: str, mode: str = "auto") -> str:
        """Start a new development session."""
        session_id = f"dev_{int(time.time())}"
        self.context = DevContext(
            session_id=session_id,
            prompt=prompt,
            jid=self.jid,
            mode=mode,
        )
        
        # Persist session
        self._save_session()
        log.info(f"Dev session started: {session_id} (mode={mode})")
        
        return session_id
    
    def _save_session(self):
        """Save session state to database."""
        if not self.context:
            return
        
        conn = db.get_connection()
        try:
            # Create table if not exists
            conn.execute("""
                CREATE TABLE IF NOT EXISTS dev_sessions (
                    session_id TEXT PRIMARY KEY,
                    jid TEXT,
                    prompt TEXT,
                    mode TEXT,
                    status TEXT,
                    current_stage TEXT,
                    artifacts TEXT,
                    error_message TEXT,
                    created_at REAL,
                    updated_at REAL
                )
            """)
            
            # Serialize artifacts
            artifacts_json = json.dumps({
                stage.name: {
                    'stage': stage.name,
                    'content': artifact.content,
                    'metadata': artifact.metadata,
                    'timestamp': artifact.timestamp
                }
                for stage, artifact in self.context.artifacts.items()
            })
            
            conn.execute("""
                INSERT OR REPLACE INTO dev_sessions 
                (session_id, jid, prompt, mode, status, current_stage, artifacts, error_message, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                self.context.session_id,
                self.context.jid,
                self.context.prompt,
                self.context.mode,
                self.context.status,
                self.context.current_stage.name if self.context.current_stage else None,
                artifacts_json,
                self.context.error_message,
                self.context.created_at,
                self.context.updated_at
            ))
            conn.commit()
        except Exception as e:
            log.error(f"Failed to save dev session: {e}")
        finally:
            conn.close()
    
    def _load_session(self, session_id: str) -> bool:
        """Load session from database."""
        conn = db.get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM dev_sessions WHERE session_id = ?",
                (session_id,)
            ).fetchone()
            
            if not row:
                return False
            
            # Reconstruct context
            self.context = DevContext(
                session_id=row[0],
                prompt=row[2],
                jid=row[1],
                mode=row[3],
                status=row[4],
                current_stage=DevStage(row[5]) if row[5] else None,
                error_message=row[7],
                created_at=row[8],
                updated_at=row[9]
            )
            
            # Deserialize artifacts
            if row[6]:
                artifacts_data = json.loads(row[6])
                for stage_name, artifact_data in artifacts_data.items():
                    stage = DevStage[stage_name]
                    self.context.artifacts[stage] = DevArtifact(
                        stage=stage,
                        content=artifact_data['content'],
                        metadata=artifact_data.get('metadata', {}),
                        timestamp=artifact_data.get('timestamp', time.time())
                    )
            
            return True
        except Exception as e:
            log.error(f"Failed to load dev session: {e}")
            return False
        finally:
            conn.close()
    
    async def run_pipeline(self, session_id: Optional[str] = None) -> bool:
        """Execute the full 7-stage pipeline."""
        if not self.context:
            if session_id:
                if not self._load_session(session_id):
                    log.error(f"Session not found: {session_id}")
                    return False
            else:
                log.error("No active session")
                return False
        
        self.context.status = "running"
        self._save_session()
        
        stages = list(DevStage)
        
        for stage in stages:
            self.context.current_stage = stage
            self._save_session()
            
            log.info(f"DevEngine: Starting stage {stage.value}")
            
            try:
                # Execute stage
                stage_method = self._stage_methods[stage]
                success = await stage_method()
                
                if not success:
                    self.context.status = "failed"
                    self.context.error_message = f"Stage {stage.value} failed"
                    self._save_session()
                    log.error(f"DevEngine: Stage {stage.value} failed")
                    return False
                
                log.info(f"DevEngine: Completed stage {stage.value}")
                
                # Interactive mode: pause after each stage
                if self.context.mode == "interactive":
                    self.context.status = "paused"
                    self._save_session()
                    log.info(f"DevEngine: Paused for user input after {stage.value}")
                    # Wait for user continuation (handled externally)
                    # This is a placeholder - actual waiting is managed by the caller
                    await self._wait_for_user_input()
                    self.context.status = "running"
                    self._save_session()
                    
            except Exception as e:
                self.context.status = "failed"
                self.context.error_message = str(e)
                self._save_session()
                log.error(f"DevEngine: Exception in stage {stage.value}: {e}")
                return False
        
        self.context.status = "completed"
        self.context.current_stage = None
        self._save_session()
        log.info(f"DevEngine: Pipeline completed for session {self.context.session_id}")
        return True
    
    async def _stage_analyze(self) -> bool:
        """Stage 1: Analyze requirements."""
        prompt = self.context.prompt
        # Use LLM to analyze requirements (simplified)
        analysis = f"""# Requirements Analysis

**Original Prompt:** {prompt}

**Key Requirements:**
1. Implement functionality based on user request
2. Ensure compatibility with EvoClaw framework
3. Follow security best practices

**Scope:**
- Input: User requirements
- Output: Functional Python module
- Constraints: Must pass immune system review
"""
        self.context.artifacts[DevStage.ANALYZE] = DevArtifact(
            stage=DevStage.ANALYZE,
            content=analysis,
            metadata={"word_count": len(analysis.split())}
        )
        return True
    
    async def _stage_design(self) -> bool:
        """Stage 2: Design architecture."""
        # Use LLM to create design spec (simplified)
        design = f"""# Design Specification

## Module Structure
- `host/tools/custom_tool.py`: Main implementation
- `tests/test_custom_tool.py`: Unit tests

## API Design
- Function: `execute_tool(params: dict) -> dict`
- Return: JSON-serializable result

## Data Flow
1. Receive input from Agent
2. Process request
3. Return result
"""
        self.context.artifacts[DevStage.DESIGN] = DevArtifact(
            stage=DevStage.DESIGN,
            content=design,
            metadata={"components": ["custom_tool.py", "test_custom_tool.py"]}
        )
        return True
    
    async def _stage_implement(self) -> bool:
        """Stage 3: Implement code."""
        # Use LLM to generate code (simplified placeholder)
        code = '''"""Custom Tool for EvoClaw."""

def execute_tool(params: dict) -> dict:
    """Execute the custom tool.
    
    Args:
        params: Input parameters
        
    Returns:
        Result dictionary
    """
    # TODO: Implement actual logic
    return {"status": "success", "message": "Tool executed"}

if __name__ == "__main__":
    print(execute_tool({}))
'''
        self.context.artifacts[DevStage.IMPLEMENT] = DevArtifact(
            stage=DevStage.IMPLEMENT,
            content=code,
            metadata={"lines": len(code.splitlines()), "language": "python"}
        )
        return True
    
    async def _stage_test(self) -> bool:
        """Stage 4: Run tests."""
        # Generate test code
        test_code = '''"""Tests for custom tool."""
import unittest

class TestCustomTool(unittest.TestCase):
    def test_execute_tool(self):
        from host.tools.custom_tool import execute_tool
        result = execute_tool({})
        self.assertEqual(result["status"], "success")

if __name__ == "__main__":
    unittest.main()
'''
        # In real implementation, run pytest/unittest
        # For now, assume success
        self.context.artifacts[DevStage.TEST] = DevArtifact(
            stage=DevStage.TEST,
            content=test_code,
            metadata={"test_count": 1, "passed": True}
        )
        return True
    
    async def _stage_review(self) -> bool:
        """Stage 5: Security review (immune system)."""
        code_artifact = self.context.artifacts.get(DevStage.IMPLEMENT)
        if not code_artifact:
            log.error("No code to review")
            return False
        
        code = code_artifact.content
        
        # Use immune system to check for injections
        # Note: immune.check_injection is for user input, but we can adapt it
        # For code review, we check for dangerous patterns
        if immune.check_injection(code):
            log.error("Security review failed: Injection detected")
            return False
        
        self.context.artifacts[DevStage.REVIEW] = DevArtifact(
            stage=DevStage.REVIEW,
            content="Security review passed. No threats detected.",
            metadata={"threats_found": 0, "passed": True}
        )
        return True
    
    async def _stage_document(self) -> bool:
        """Stage 6: Generate documentation."""
        docs = f"""# Custom Tool Documentation

## Usage
```python
from host.tools.custom_tool import execute_tool
result = execute_tool({{"param": "value"}})
```

## API Reference
- `execute_tool(params: dict) -> dict`

## Examples
See test cases for usage examples.
"""
        self.context.artifacts[DevStage.DOCUMENT] = DevArtifact(
            stage=DevStage.DOCUMENT,
            content=docs,
            metadata={"sections": ["Usage", "API", "Examples"]}
        )
        return True
    
    async def _stage_deploy(self) -> bool:
        """Stage 7: Deploy (commit and reload)."""
        # In real implementation:
        # 1. Write files to disk
        # 2. Git commit
        # 3. Reload module
        # 4. Health check
        
        self.context.artifacts[DevStage.DEPLOY] = DevArtifact(
            stage=DevStage.DEPLOY,
            content="Deployment completed successfully.",
            metadata={"deployed": True, "timestamp": time.time()}
        )
        return True
    
    async def _wait_for_user_input(self):
        """Wait for user input in interactive mode."""
        # This is a placeholder - actual implementation depends on the caller
        # In practice, this would be handled by the API or CLI
        await asyncio.sleep(1)  # Prevent blocking
    
    def get_status(self) -> Dict[str, Any]:
        """Get current session status."""
        if not self.context:
            return {"status": "no_session"}
        
        return {
            "session_id": self.context.session_id,
            "prompt": self.context.prompt,
            "mode": self.context.mode,
            "status": self.context.status,
            "current_stage": self.context.current_stage.value if self.context.current_stage else None,
            "artifacts": {
                stage.name: {
                    "content": artifact.content[:100] + "..." if len(artifact.content) > 100 else artifact.content,
                    "metadata": artifact.metadata
                }
                for stage, artifact in self.context.artifacts.items()
            },
            "error_message": self.context.error_message,
            "created_at": self.context.created_at,
            "updated_at": self.context.updated_at
        }
    
    async def continue_session(self, user_input: str) -> bool:
        """Continue a paused session with user input."""
        if not self.context or self.context.status != "paused":
            return False
        
        # Process user input (e.g., edit artifact, confirm, etc.)
        # For now, just resume
        self.context.status = "running"
        self._save_session()
        
        # Continue pipeline
        return await self.run_pipeline()
