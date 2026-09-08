"""Verify the ClickUp credentials in .env and print what the agent can see.

Run with:  uv run python scripts/check_clickup.py
"""

import os
import sys
from datetime import UTC, datetime

import httpx
from dotenv import load_dotenv

load_dotenv()

BASE = "https://api.clickup.com/api/v2"
TOKEN = os.getenv("CLICKUP_API_TOKEN", "")
TEAM_ID = os.getenv("CLICKUP_TEAM_ID", "")


def fail(msg: str) -> None:
    print(f"\n  FAILED: {msg}")
    sys.exit(1)


def ms_to_date(ms: str | None) -> str:
    if not ms:
        return "no due date"
    return datetime.fromtimestamp(int(ms) / 1000, UTC).strftime("%Y-%m-%d")


def main() -> None:
    if not TOKEN or TOKEN == "pk_":
        fail("CLICKUP_API_TOKEN is not set in .env")
    if not TEAM_ID:
        fail("CLICKUP_TEAM_ID is not set in .env")

    client = httpx.Client(base_url=BASE, headers={"Authorization": TOKEN}, timeout=20)

    # 1. Who am I?
    r = client.get("/user")
    if r.status_code == 401:
        fail("token rejected (401) — check CLICKUP_API_TOKEN")
    r.raise_for_status()
    user = r.json()["user"]
    print(f"\nAuthenticated as : {user['username']} <{user['email']}>")
    print(f"Your user id     : {user['id']}   <- goes in CLICKUP_USER_ID")

    # 2. Walk the hierarchy: spaces -> lists
    spaces = client.get(f"/team/{TEAM_ID}/space", params={"archived": "false"}).json()["spaces"]
    print(f"\nSpaces ({len(spaces)}):")

    lists: list[dict] = []
    for space in spaces:
        print(f"  - {space['name']}  (id {space['id']})")
        folderless = client.get(f"/space/{space['id']}/list", params={"archived": "false"})
        for lst in folderless.json()["lists"]:
            lists.append(lst)
            print(f"      list: {lst['name']}  (id {lst['id']})")
        for folder in client.get(f"/space/{space['id']}/folder").json()["folders"]:
            for lst in folder["lists"]:
                lists.append(lst)
                print(f"      list: {folder['name']} / {lst['name']}  (id {lst['id']})")

    if not lists:
        fail("no lists found — create a List and put tasks in it")

    # 3. Statuses, grouped by type (the agent reasons about type, not name)
    statuses = client.get(f"/list/{lists[0]['id']}").json()["statuses"]
    print(f"\nStatuses on '{lists[0]['name']}':")
    for s in statuses:
        print(f"  - {s['status']:<20} type={s['type']}")

    # 4. Tasks assigned to me
    r = client.get(
        f"/team/{TEAM_ID}/task",
        params={"assignees[]": user["id"], "include_closed": "true", "subtasks": "true"},
    )
    r.raise_for_status()
    tasks = r.json()["tasks"]
    print(f"\nTasks assigned to you: {len(tasks)}")
    for t in tasks[:15]:
        prio = (t.get("priority") or {}).get("priority", "none")
        print(f"  [{t['status']['status']:<12}] {t['name'][:45]:<45} due {ms_to_date(t.get('due_date'))}  prio {prio}")

    print("\nClickUp connection OK.\n")


if __name__ == "__main__":
    main()
