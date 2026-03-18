"""
Jira Integration — Phase 3 Enterprise Suite

Provides issue create/query/update/comment for agent-driven workflows.
"""
import os
import json
import logging
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


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
        self.api_token = api_token or os.getenv("JIRA_API_TOKEN", "")
        self.project = project or os.getenv("JIRA_PROJECT", "")
        self._session = None
        if self.base_url and self.email and self.api_token:
            self._init_session()

    def _init_session(self):
        try:
            import requests
            from requests.auth import HTTPBasicAuth
            self._session = requests.Session()
            self._session.auth = HTTPBasicAuth(self.email, self.api_token)
            self._session.headers.update({"Content-Type": "application/json"})
            logger.info(f"Jira connected: {self.base_url}")
        except ImportError:
            logger.warning("requests not installed — Jira connector unavailable")

    def _get(self, path: str, params: Optional[Dict] = None) -> Optional[Dict]:
        if not self._session:
            return None
        try:
            r = self._session.get(f"{self.base_url}/rest/api/3{path}", params=params)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.error(f"Jira GET {path} failed: {e}")
            return None

    def _post(self, path: str, data: Dict) -> Optional[Dict]:
        if not self._session:
            return None
        try:
            r = self._session.post(f"{self.base_url}/rest/api/3{path}", json=data)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.error(f"Jira POST {path} failed: {e}")
            return None

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
        """Fetch a Jira issue by key."""
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
        """Search issues with JQL."""
        data = self._get("/search", {"jql": jql, "maxResults": max_results})
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
        """Add a comment to an issue."""
        payload = {
            "body": {
                "type": "doc", "version": 1,
                "content": [{"type": "paragraph", "content": [{"type": "text", "text": comment}]}]
            }
        }
        result = self._post(f"/issue/{key}/comment", payload)
        return result is not None

    def transition_issue(self, key: str, status_name: str) -> bool:
        """Transition an issue to a new status."""
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
        return bool(self.base_url and self.email and self.api_token)
