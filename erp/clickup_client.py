"""
Client for calling the ClickUp API to create tasks and pull back comments.
If CLICKUP_API_TOKEN / CLICKUP_LIST_ID aren't configured -> automatically runs in
mock mode (returns a fake task) so the whole pipeline still runs end-to-end
during dev/test.
"""
import logging
import uuid

import requests

from erp.config import CLICKUP_API_TOKEN, CLICKUP_LIST_ID

logger = logging.getLogger(__name__)

BASE_URL = "https://api.clickup.com/api/v2"


def is_configured() -> bool:
    return bool(CLICKUP_API_TOKEN and CLICKUP_LIST_ID)


def create_task(name: str, description: str, assignee_clickup_id: str | None = None) -> dict:
    """Create a ClickUp task. Returns a dict with at least an 'id' field."""
    if not is_configured():
        mock_id = f"MOCK-{uuid.uuid4().hex[:8]}"
        logger.warning(
            f"ClickUp is not fully configured (missing token or list id) - "
            f"creating the task in mock mode with id={mock_id}."
        )
        return {"id": mock_id, "mocked": True}

    payload = {"name": name, "description": description}
    if assignee_clickup_id:
        payload["assignees"] = [assignee_clickup_id]

    headers = {"Authorization": CLICKUP_API_TOKEN, "Content-Type": "application/json"}
    response = requests.post(
        f"{BASE_URL}/list/{CLICKUP_LIST_ID}/task",
        json=payload,
        headers=headers,
        timeout=10,
    )
    response.raise_for_status()
    data = response.json()
    data["mocked"] = False
    return data


def get_team_members() -> list[dict]:
    """Get the member list for every workspace (team) the token has access to."""
    if not CLICKUP_API_TOKEN:
        return []

    headers = {"Authorization": CLICKUP_API_TOKEN}
    response = requests.get(f"{BASE_URL}/team", headers=headers, timeout=10)
    response.raise_for_status()

    members = []
    for team in response.json().get("teams", []):
        for m in team.get("members", []):
            user = m.get("user", {})
            members.append({
                "id": user.get("id"),
                "username": user.get("username"),
                "email": user.get("email"),
                "team_name": team.get("name"),
            })
    return members


def get_task_comments(task_id: str) -> list[dict]:
    """Get the comment list for one task. A mock task returns an empty list."""
    if not CLICKUP_API_TOKEN or task_id.startswith("MOCK-"):
        return []

    headers = {"Authorization": CLICKUP_API_TOKEN}
    response = requests.get(
        f"{BASE_URL}/task/{task_id}/comment", headers=headers, timeout=10
    )
    response.raise_for_status()
    return response.json().get("comments", [])
