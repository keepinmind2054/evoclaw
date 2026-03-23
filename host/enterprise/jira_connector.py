"""
Jira Integration — Phase 3 Enterprise Suite

Provides issue create/query/update/comment for agent-driven workflows.
"""
import os
import re
import logging
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Maximum number of Jira issues that search_issues() will ever return.
# Jira's own maxResults cap is 1000; we enforce a tighter bot-side limit to
# prevent millions of items from blowing up the LLM context window or RAM.
_MAX_SEARCH_RESULTS = 500

# Allowlist for Jira issue keys: PROJECT-123 style only.
# Prevents path-traversal in REST URLs like /issue/../admin
_ISSUE_KEY_RE = re.compile(r'^[A-Z][A-Z0-9_]{0,9}-[0-9]{1,7}$')

# Allowlist for Jira project keys.
_PROJECT_KEY_RE = re.compile(r'^[A-Z][A-Z0-9_]{0,9}$')


def _validate_issue_key(key: str) -> str:
    """Raise ValueError if the issue key does not look like PROJECT-123."""
    if not _ISSUE_KEY_RE.match(key):
        raise ValueError(f"Invalid Jira issue key: {key!r}")
    return key


def _validate_project_key(key: str) -> str:
    """Raise ValueError if the project key is not safe."""
    if not _PROJECT_KEY_RE.match(key):
        raise ValueError(f"Invalid Jira project key: {key!r}")
    return key


@dataclass
class JiraIssue:
    key: str
    summary: str
    status: str
    assignee: Optional[str] = None
    priority: str = "Medium"
    description: str = ""
    labels: List[str] = field(default_factory=list)
    url: str = ""


class JiraConnector:
    """
    Jira REST API connector for agent-driven issue management.

    Config via environment:
      JIRA_BASE_URL   — e.g. https://mycompany.atlassian.net
      JIRA_EMAIL      — user email
      JIRA_API_TOKEN  — API token from Atlassian
      JIRA_PROJECT    — default project key
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        email: Optional[str] = None,
        api_token: Optional[str] = None,
        project: Optional[str] = None,
    ):
        self.base_url = (base_url or os.getenv("JIRA_BASE_URL", "")).rstrip("/")
        self.email = email or os.getenv("JIRA_EMAIL", "")
        # P14D-JIRA-1: token stored only in instance attribute, never logged
        self._api_token = api_token or os.getenv("JIRA_API_TOKEN", "")
        self.project = project or os.getenv("JIRA_PROJECT", "")
        self._session = None
        if self.base_url and self.email and self._api_token:
            self._init_session()

    def _init_session(self):
        try:
            import requests
            from requests.auth import HTTPBasicAuth
            self._session = requests.Session()
            # P14D-JIRA-1: credentials used directly in auth object, never serialised
            self._session.auth = HTTPBasicAuth(self.email, self._api_token)
            self._session.headers.update({"Content-Type": "application/json"})
            # P14D-JIRA-4: do NOT log the token or credentials
            logger.info(f"Jira connected: {self.base_url}")
        except ImportError:
            logger.warning("requests not installed — Jira connector unavailable")

    # ------------------------------------------------------------------
    # Internal HTTP helpers
    # ------------------------------------------------------------------

    def _get(self, path: str, params: Optional[Dict] = None) -> Optional[Dict]:
        """HTTP GET with error handling.

        P14D-JIRA-2: All API calls are wrapped in try/except so network errors
        surface as None rather than propagating exceptions to callers.
        """
        if not self._session:
            return None
        try:
            r = self._session.get(
                f"{self.base_url}/rest/api/3{path}",
                params=params,
                timeout=30,  # P14D-JIRA-5: explicit timeout on every request
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.error(f"Jira GET {path} failed: {e}")
            return None

    def _post(self, path: str, data: Dict) -> Optional[Dict]:
        """HTTP POST with error handling.

        P14D-JIRA-2 / P14D-JIRA-5: same safety measures as _get().
        """
        if not self._session:
            return None
        try:
            r = self._session.post(
                f"{self.base_url}/rest/api/3{path}",
                json=data,
                timeout=30,
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.error(f"Jira POST {path} failed: {e}")
            return None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_issue(
        self,
        summary: str,
        description: str = "",
        issue_type: str = "Task",
        priority: str = "Medium",
        labels: Optional[List[str]] = None,
        project: Optional[str] = None,
    ) -> Optional[JiraIssue]:
        """Create a Jira issue and return the created issue."""
        proj = project or self.project
        if not proj:
            logger.error("No Jira project configured")
            return None
        # P14D-JIRA-3: validate the project key so it cannot be used for path traversal
        try:
            _validate_project_key(proj)
        except ValueError as e:
            logger.error(f"create_issue: {e}")
            return None
        payload = {
            "fields": {
                "project": {"key": proj},
                "summary": summary,
                "description": {
                    "type": "doc", "version": 1,
                    "content": [{"type": "paragraph", "content": [{"type": "text", "text": description}]}]
                },
                "issuetype": {"name": issue_type},
                "priority": {"name": priority},
            }
        }
        if labels:
            payload["fields"]["labels"] = labels
        result = self._post("/issue", payload)
        if result:
            return JiraIssue(
                key=result["key"],
                summary=summary,
                status="To Do",
                description=description,
                priority=priority,
                labels=labels or [],
                url=f"{self.base_url}/browse/{result['key']}",
            )
        return None

    def get_issue(self, key: str) -> Optional[JiraIssue]:
        """Fetch a Jira issue by key.

        P14D-JIRA-3: key is validated to prevent path-traversal in the REST URL.
        """
        try:
            _validate_issue_key(key)
        except ValueError as e:
            logger.error(f"get_issue: {e}")
            return None
        data = self._get(f"/issue/{key}")
        if not data:
            return None
        fields = data.get("fields", {})
        return JiraIssue(
            key=data["key"],
            summary=fields.get("summary", ""),
            status=fields.get("status", {}).get("name", ""),
            assignee=(fields.get("assignee") or {}).get("displayName"),
            priority=(fields.get("priority") or {}).get("name", "Medium"),
            description=str(fields.get("description") or ""),
            labels=fields.get("labels", []),
            url=f"{self.base_url}/browse/{data['key']}",
        )

    def search_issues(self, jql: str, max_results: int = 20) -> List[JiraIssue]:
        """Search issues with JQL.

        P14D-JIRA-6: cap max_results to _MAX_SEARCH_RESULTS so a pathological
        JQL query (or a caller passing max_results=10**9) cannot pull millions
        of items into memory.
        """
        capped = min(max_results, _MAX_SEARCH_RESULTS)
        if capped != max_results:
            logger.warning(
                f"search_issues: max_results={max_results} capped to {_MAX_SEARCH_RESULTS}"
            )
        data = self._get("/search", {"jql": jql, "maxResults": capped})
        if not data:
            return []
        issues = []
        for item in data.get("issues", []):
            fields = item.get("fields", {})
            issues.append(JiraIssue(
                key=item["key"],
                summary=fields.get("summary", ""),
                status=(fields.get("status") or {}).get("name", ""),
                assignee=(fields.get("assignee") or {}).get("displayName"),
                priority=(fields.get("priority") or {}).get("name", "Medium"),
                labels=fields.get("labels", []),
                url=f"{self.base_url}/browse/{item['key']}",
            ))
        return issues

    def add_comment(self, key: str, comment: str) -> bool:
        """Add a comment to an issue.

        P14D-JIRA-3: issue key validated before use in URL.
        """
        try:
            _validate_issue_key(key)
        except ValueError as e:
            logger.error(f"add_comment: {e}")
            return False
        payload = {
            "body": {
                "type": "doc", "version": 1,
                "content": [{"type": "paragraph", "content": [{"type": "text", "text": comment}]}]
            }
        }
        result = self._post(f"/issue/{key}/comment", payload)
        return result is not None

    def transition_issue(self, key: str, status_name: str) -> bool:
        """Transition an issue to a new status.

        P14D-JIRA-3: issue key validated before use in URL.
        """
        try:
            _validate_issue_key(key)
        except ValueError as e:
            logger.error(f"transition_issue: {e}")
            return False
        transitions = self._get(f"/issue/{key}/transitions")
        if not transitions:
            return False
        for t in transitions.get("transitions", []):
            if t["name"].lower() == status_name.lower():
                result = self._post(f"/issue/{key}/transitions", {"transition": {"id": t["id"]}})
                return result is not None
        logger.warning(f"Transition '{status_name}' not found for {key}")
        return False

    def is_configured(self) -> bool:
        return bool(self.base_url and self.email and self._api_token)
