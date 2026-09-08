"""Verify the Slack credentials in .env and find your Slack user id.

Run with:  uv run python scripts/check_slack.py
"""

import os
import sys

from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

load_dotenv()

BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
APP_TOKEN = os.getenv("SLACK_APP_TOKEN", "")


def fail(msg: str) -> None:
    print(f"\n  FAILED: {msg}")
    sys.exit(1)


def main() -> None:
    if not BOT_TOKEN.startswith("xoxb-"):
        fail("SLACK_BOT_TOKEN missing or does not start with xoxb-")
    if not APP_TOKEN.startswith("xapp-"):
        fail("SLACK_APP_TOKEN missing or does not start with xapp-")

    client = WebClient(token=BOT_TOKEN)

    # 1. Bot token valid?
    try:
        auth = client.auth_test()
    except SlackApiError as e:
        fail(f"bot token rejected: {e.response['error']}")
    print(f"\nWorkspace : {auth['team']}")
    print(f"Bot       : {auth['user']} ({auth['user_id']})")

    # 2. Granted scopes
    scopes = client.api_call("auth.test").headers.get("x-oauth-scopes", "")
    print(f"Scopes    : {scopes}")

    # 3. App-level token valid? (this is what Socket Mode dials out with)
    try:
        WebClient(token=APP_TOKEN).api_call("apps.connections.open")
        print("App token : valid (Socket Mode can connect)")
    except SlackApiError as e:
        fail(f"app token rejected: {e.response['error']}")

    # 4. Find the human members — one of these is you
    members = [
        m
        for m in client.users_list()["members"]
        if not m.get("is_bot") and not m.get("deleted") and m["id"] != "USLACKBOT"
    ]
    print(f"\nHuman members ({len(members)}):")
    for m in members:
        print(f"  {m['id']}   {m.get('real_name', m['name'])}")

    if len(members) == 1:
        print(f"\nSLACK_USER_ID={members[0]['id']}   <- only human here, that's you")

    print("\nSlack connection OK.\n")


if __name__ == "__main__":
    main()
