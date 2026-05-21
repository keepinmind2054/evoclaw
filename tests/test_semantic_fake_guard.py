"""
Regression test for the SEMANTIC-FAKE / FAKE-STATUS false-positive that made
the agent dispatch ``"（處理完成，但未能產生文字回應，請重新詢問。）"`` on every
multi-turn task with a Chinese closing sentence.

Bug:
    `_loop_gemini.py:200`, `_loop_openai.py:527`, `_loop_claude.py:149` wiped
    ``final_response`` whenever ``_ACTION_CLAIM_RE`` matched on a text-only
    model turn.  But the very-last turn of a healthy agentic run is *always*
    text-only by design (summary of the prior tool turns), so any closing
    sentence containing "已建立 / 已完成 / 已部署 / Successfully completed …"
    was wiped → loop ran to MAX_ITER → empty final_response → fallback string.

Fix:
    `_constants.is_unverified_action_claim(text, substantive_action_count)`
    only returns True when ``substantive_action_count == 0`` (zero substantive
    tool calls in the run), so closing summaries that *follow* real work are
    correctly allowed through.

The three loop files import this helper directly; tests below pin the helper's
contract so regressions show up at unit-test time instead of in production at
30 s/turn × 20 turns later.
"""
import sys
from pathlib import Path

# The agent runner lives in container/agent-runner/ and is not a package — add
# its directory to sys.path so `import _constants` resolves the same way the
# loop modules do at runtime.
_AGENT_DIR = Path(__file__).parent.parent / "container" / "agent-runner"
sys.path.insert(0, str(_AGENT_DIR))

from _constants import is_unverified_action_claim, _ACTION_CLAIM_RE


class TestUnverifiedClaimGuard:
    """Cases where the helper must allow the text through (return False)."""

    def test_legit_closing_summary_after_real_work_allowed(self):
        # The exact scenario from the bug report: agent built a skill via
        # Write + Read + Bash, then summarised in the final turn.
        text = "MCP 安全檢查 skill 已建立完成，包含 SKILL.md 和 handler.py。"
        assert is_unverified_action_claim(text, substantive_action_count=3) is False

    def test_english_closing_summary_after_real_work_allowed(self):
        text = "Successfully completed the fix and committed the patch."
        assert is_unverified_action_claim(text, substantive_action_count=5) is False

    def test_chinese_verb_le_pattern_after_real_work_allowed(self):
        text = "我更新了 README.md，新增了三段使用範例。"
        assert is_unverified_action_claim(text, substantive_action_count=2) is False

    def test_text_without_completion_claim_allowed(self):
        text = "您好！有什麼可以幫您的嗎？"
        assert is_unverified_action_claim(text, substantive_action_count=0) is False


class TestUnverifiedClaimGuardCatches:
    """Cases where the helper must still flag the text (return True)."""

    def test_zero_work_claim_caught_chinese(self):
        # The original hallucination pattern: agent claims completion without
        # having called any substantive tool — that is the spam we DO want to
        # catch.  Helper must still flag this run.
        text = "我已建立 MCP 安全檢查 skill 並完成測試。"
        assert is_unverified_action_claim(text, substantive_action_count=0) is True

    def test_zero_work_claim_caught_english(self):
        text = "I have successfully completed the fix."
        # Helper checks _ACTION_CLAIM_RE, which requires the
        # `successfully completed the fix|update|feature|...` shape.
        assert is_unverified_action_claim(text, substantive_action_count=0) is True

    def test_zero_work_claim_caught_le_suffix(self):
        text = "部署了新版本。"
        assert is_unverified_action_claim(text, substantive_action_count=0) is True


class TestRegexIntent:
    """Pin the regex so refactors that loosen / narrow it surface here."""

    def test_regex_matches_each_target_verb(self):
        for verb in [
            "已完成", "已修復", "已部署", "已更新", "已新增", "已刪除",
            "已建立", "已創建", "已執行", "已安裝", "已設定", "已提交",
            "已推送", "已合併",
            "完成了", "修復了", "部署了", "新增了", "建立了",
        ]:
            assert _ACTION_CLAIM_RE.search(verb), f"expected match: {verb}"

    def test_regex_ignores_unrelated_text(self):
        for benign in [
            "您好",
            "請問需要什麼協助？",
            "Hello world",
            # The previously-fixed false positives from BUG-P21-1:
            "我已了解您的問題",   # 已+了 (not action verb)
            "成功的範例如下",     # successful without action verb
        ]:
            assert not _ACTION_CLAIM_RE.search(benign), f"unexpected match: {benign}"
