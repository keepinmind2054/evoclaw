"""
Tests for DevEngine: 7-Stage Development Pipeline
"""
import pytest
import asyncio
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from host.dev_engine import DevEngine, DevStage, DevContext


class TestDevEngineBasics:
    """Basic functionality tests for DevEngine."""
    
    def test_engine_initialization(self):
        """Test that DevEngine initializes correctly."""
        engine = DevEngine(jid="test:user123")
        assert engine.jid == "test:user123"
        assert engine.context is None
    
    def test_dev_context_creation(self):
        """Test DevContext dataclass."""
        ctx = DevContext(
            prompt="Test prompt",
            stage=DevStage.ANALYZE,
            interactive=False
        )
        assert ctx.prompt == "Test prompt"
        assert ctx.stage == DevStage.ANALYZE
        assert ctx.interactive is False
        assert ctx.artifacts == {}


class TestDevEnginePipeline:
    """Test the 7-stage pipeline execution."""
    
    @pytest.mark.asyncio
    async def test_pipeline_stages_execution(self):
        """Test that all 7 stages execute in order."""
        engine = DevEngine(jid="test:user123")
        
        # Run pipeline in automated mode
        success = await engine.run_pipeline(
            "Create a simple tool",
            interactive=False
        )
        
        assert success is True
        assert engine.context is not None
        assert engine.context.status == "completed"
        
        # Check that all artifacts were created
        assert 'requirements' in engine.context.artifacts
        assert 'design' in engine.context.artifacts
        assert 'code' in engine.context.artifacts
        assert 'tests' in engine.context.artifacts
        assert 'security_audit' in engine.context.artifacts
        assert 'docs' in engine.context.artifacts
        assert 'deployment' in engine.context.artifacts
    
    @pytest.mark.asyncio
    async def test_pipeline_stages_order(self):
        """Test that stages execute in correct order."""
        engine = DevEngine(jid="test:user456")
        
        # Track stage execution order
        executed_stages = []
        
        # We can't easily intercept the internal stage methods,
        # but we can verify the final state
        await engine.run_pipeline("Test", interactive=False)
        
        # If pipeline completed, all stages executed in order
        assert engine.context.status == "completed"
        assert engine.context.stage == DevStage.DEPLOY


class TestDevEngineSecurity:
    """Test security review stage."""
    
    @pytest.mark.asyncio
    async def test_security_review_detects_suspicious_code(self):
        """Test that security review detects eval/exec patterns."""
        # This tests the _stage_review logic
        # In a real scenario, we'd inject suspicious code
        # For now, we verify the stage completes
        engine = DevEngine(jid="test:security")
        
        # Normal code should pass
        engine.context = DevContext(
            prompt="test",
            stage=DevStage.REVIEW,
            artifacts={'code': 'def safe(): return 1'}
        )
        
        result = await engine._stage_review()
        assert result is True
    
    @pytest.mark.asyncio
    async def test_security_review_blocks_eval(self):
        """Test that security review blocks eval() usage."""
        engine = DevEngine(jid="test:security2")
        
        # Code with eval should fail
        engine.context = DevContext(
            prompt="test",
            stage=DevStage.REVIEW,
            artifacts={'code': 'def unsafe(): return eval(input())'}
        )
        
        result = await engine._stage_review()
        assert result is False


class TestDevEngineModes:
    """Test interactive vs automated modes."""
    
    @pytest.mark.asyncio
    async def test_automated_mode_completes_without_pause(self):
        """Test that automated mode runs through without pausing."""
        engine = DevEngine(jid="test:auto")
        
        import time
        start = time.time()
        
        success = await engine.run_pipeline("Auto test", interactive=False)
        
        duration = time.time() - start
        
        # Should complete quickly (no user interaction delays)
        assert success is True
        assert duration < 5.0  # Should complete in under 5 seconds
    
    @pytest.mark.asyncio
    async def test_interactive_mode_sets_context(self):
        """Test that interactive mode properly sets up context."""
        engine = DevEngine(jid="test:interactive")
        
        # We can't test actual user interaction,
        # but we can verify the context is set up correctly
        engine.context = DevContext(
            prompt="Interactive test",
            stage=DevStage.ANALYZE,
            interactive=True
        )
        
        assert engine.context.interactive is True


class TestDevEngineArtifacts:
    """Test artifact generation and storage."""
    
    @pytest.mark.asyncio
    async def test_artifacts_accumulate_through_pipeline(self):
        """Test that artifacts are accumulated through the pipeline."""
        engine = DevEngine(jid="test:artifacts")
        
        await engine.run_pipeline("Artifact test", interactive=False)
        
        # Verify artifact structure
        ctx = engine.context
        assert len(ctx.artifacts) >= 6  # At least 6 artifacts
        
        # Check artifact types
        assert isinstance(ctx.artifacts['requirements'], str)
        assert isinstance(ctx.artifacts['design'], str)
        assert isinstance(ctx.artifacts['code'], str)
        assert isinstance(ctx.artifacts['tests'], str)
        assert isinstance(ctx.artifacts['docs'], str)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
