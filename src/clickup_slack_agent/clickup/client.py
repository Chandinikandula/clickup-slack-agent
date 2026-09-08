"""HTTP client for the ClickUp v2 API.

Both halves of the app go through this: the digest calls it directly, the
agent calls it via tool schemas. Keeping it in one place means the agent
cannot reach ClickUp in ways the digest hasn't already exercised.
"""

import logging
import time
from datetime import datetime

import httpx

from .models import Comment, Status, Task, TaskList

log = logging.getLogger(__name__)

BASE_URL = "https://api.clickup.com/api/v2"
PAGE_SIZE = 100  # ClickUp's fixed page size for the team task endpoint
MAX_PAGES = 20  # guard against paging forever on an unexpected response
MAX_RETRIES = 3


class ClickUpError(RuntimeError):
    """Raised when ClickUp refuses a request we cannot recover from."""


class ClickUpClient:
    def __init__(
        self,
        token: str,
        team_id: str,
        *,
        exclude_list_ids: frozenset[str] = frozenset(),
        include_list_ids: frozenset[str] = frozenset(),
        timeout: float = 20.0,
    ) -> None:
        self.team_id = team_id
        self.exclude_list_ids = exclude_list_ids
        self.include_list_ids = include_list_ids
        self._http = httpx.Client(
            base_url=BASE_URL,
            headers={"Authorization": token, "Content-Type": "application/json"},
            timeout=timeout,
        )

    # -- plumbing ---------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs) -> dict:
        """Issue a request, retrying on rate limits and transient 5xx.

        ClickUp allows 100 requests/minute per token and answers 429 with a
        Retry-After header. Without this the agent would surface a raw stack
        trace to Slack the first time it made a few calls in quick succession.
        """
        for attempt in range(MAX_RETRIES):
            r = self._http.request(method, path, **kwargs)

            if r.status_code == 429:
                wait = float(r.headers.get("Retry-After", 2**attempt))
                log.warning("clickup rate limited, sleeping %.1fs", wait)
                time.sleep(wait)
                continue

            if r.status_code >= 500 and attempt < MAX_RETRIES - 1:
                time.sleep(2**attempt)
                continue

            if r.status_code == 401:
                raise ClickUpError("ClickUp rejected the API token (401)")
            if r.status_code >= 400:
                raise ClickUpError(f"ClickUp {r.status_code} on {path}: {r.text[:200]}")

            return r.json()

        raise ClickUpError(f"ClickUp still rate limiting after {MAX_RETRIES} attempts")

    def _keep(self, list_id: str) -> bool:
        """Whether a task in this list belongs in our results.

        An allowlist wins if set; otherwise everything survives except the
        explicitly excluded lists. Defaulting to "include" means a new Space
        shows up on its own — a missing task is a worse failure than a noisy one.
        """
        if self.include_list_ids:
            return list_id in self.include_list_ids
        return list_id not in self.exclude_list_ids

    # -- reads ------------------------------------------------------------

    def get_current_user(self) -> dict:
        return self._request("GET", "/user")["user"]

    def get_tasks(
        self,
        *,
        assignee_ids: list[int] | None = None,
        include_closed: bool = False,
        due_before: datetime | None = None,
        due_after: datetime | None = None,
    ) -> list[Task]:
        """Tasks across the whole workspace, filtered server-side where possible."""
        params: dict[str, object] = {
            "include_closed": str(include_closed).lower(),
            "subtasks": "true",
        }
        if assignee_ids:
            params["assignees[]"] = [str(i) for i in assignee_ids]
        if due_before:
            params["due_date_lt"] = int(due_before.timestamp() * 1000)
        if due_after:
            params["due_date_gt"] = int(due_after.timestamp() * 1000)

        tasks: list[Task] = []
        for page in range(MAX_PAGES):
            payload = self._request(
                "GET", f"/team/{self.team_id}/task", params={**params, "page": page}
            )
            batch = payload.get("tasks", [])
            tasks.extend(Task.from_api(t) for t in batch)
            if len(batch) < PAGE_SIZE or payload.get("last_page"):
                break
        else:
            log.warning("stopped paging at %d pages; results may be truncated", MAX_PAGES)

        return [t for t in tasks if self._keep(t.list_id)]

    def get_task(self, task_id: str) -> Task:
        return Task.from_api(self._request("GET", f"/task/{task_id}"))

    def get_comments(self, task_id: str, limit: int = 20) -> list[Comment]:
        raw = self._request("GET", f"/task/{task_id}/comment")["comments"]
        return [Comment.from_api(c) for c in raw[:limit]]

    def get_lists(self) -> list[TaskList]:
        """Every list in the workspace, folderless and foldered alike."""
        out: list[TaskList] = []
        spaces = self._request("GET", f"/team/{self.team_id}/space", params={"archived": "false"})[
            "spaces"
        ]

        for space in spaces:
            sid, sname = space["id"], space["name"]
            for lst in self._request("GET", f"/space/{sid}/list", params={"archived": "false"})[
                "lists"
            ]:
                out.append(TaskList(id=str(lst["id"]), name=lst["name"], space_name=sname))
            for folder in self._request("GET", f"/space/{sid}/folder")["folders"]:
                for lst in folder["lists"]:
                    out.append(
                        TaskList(
                            id=str(lst["id"]),
                            name=f"{folder['name']} / {lst['name']}",
                            space_name=sname,
                        )
                    )
        return [lst for lst in out if self._keep(lst.id)]

    def get_statuses(self, list_id: str) -> list[Status]:
        """Statuses available on a list — fetched live, never hardcoded."""
        raw = self._request("GET", f"/list/{list_id}")["statuses"]
        return [Status.model_validate(s) for s in raw]

    # -- writes -----------------------------------------------------------

    def update_status(self, task_id: str, status: str) -> Task:
        return Task.from_api(self._request("PUT", f"/task/{task_id}", json={"status": status}))

    def add_comment(self, task_id: str, text: str, notify_all: bool = False) -> str:
        payload = self._request(
            "POST",
            f"/task/{task_id}/comment",
            json={"comment_text": text, "notify_all": notify_all},
        )
        return payload.get("id", "")

    def close(self) -> None:
        self._http.close()
