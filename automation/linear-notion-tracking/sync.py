#!/usr/bin/env python3
"""
sync.py — One-way Linear -> Notion Issue Mirror
Mirror issues from Linear (Team TAI) into Notion database (Linear Tracking).
"""

import argparse
from datetime import datetime, timezone
import json
import logging
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

LINEAR_TEAM_ID = "6359be10-ea15-4972-80bf-7eac98a64835"
NOTION_DATABASE_ID = "4000a859-ed1f-8051-b991-f76de0549b2b"
NOTION_API_VERSION = "2022-06-28"

STATE_MACHINE_OPTIONS = {
    "agent:context",
    "human:context-review",
    "agent:planning",
    "human:plan-review",
    "agent:ready-to-execute",
    "agent:executing",
    "agent:verifying",
    "human:result-review",
    "agent:merging",
    "agent:needs-input",
    "agent:blocked",
}

INFRASTRUCTURE_OPTIONS = {
    "Build",
    "Refactor",
    "Integration",
    "Evaluation",
    "Migration",
    "Maintenance",
}

PRIORITY_MAP = {
    0: "No priority",
    1: "Urgent",
    2: "High",
    3: "Medium",
    4: "Low",
}

STATUS_MAP = {
    "backlog": "Backlog",
    "todo": "Todo",
    "in progress": "In Progress",
    "started": "In Progress",
    "in review": "In Review",
    "done": "Done",
    "completed": "Done",
    "canceled": "Canceled",
    "cancelled": "Canceled",
    "duplicate": "Duplicate",
}


REGISTRY_PATH = os.path.join(os.path.dirname(__file__), "projects.json")


def load_project_registry() -> dict:
    """Load registered active projects from projects.json."""
    if not os.path.exists(REGISTRY_PATH):
        return {}
    try:
        with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            projects = data.get("projects", {})
            return {k: v for k, v in projects.items() if v.get("active", True)}
    except Exception as e:
        logging.warning(f"Could not load project registry from {REGISTRY_PATH}: {e}")
        return {}


def get_linear_token() -> str:
    token = os.environ.get("LINEAR_API_KEY")
    if token:
        return token
    try:
        res = subprocess.run(["linear", "auth", "token"], capture_output=True, text=True, check=True)
        return res.stdout.strip()
    except Exception as e:
        raise RuntimeError(f"Failed to obtain Linear API token: {e}")


def get_notion_token() -> str:
    token = os.environ.get("NOTION_API_TOKEN")
    if token:
        return token
    try:
        res = subprocess.run(["ntn", "auth", "token"], capture_output=True, text=True, check=True)
        return res.stdout.strip()
    except Exception as e:
        raise RuntimeError(f"Failed to obtain Notion API token: {e}")


def api_request(url: str, headers: dict, data: dict = None, method: str = "GET", max_retries: int = 3) -> dict:
    body = json.dumps(data).encode("utf-8") if data is not None else None
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method=method)
            with urllib.request.urlopen(req) as resp:
                res_bytes = resp.read()
                return json.loads(res_bytes.decode("utf-8")) if res_bytes else {}
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            if e.code == 429 and attempt < max_retries - 1:
                wait_sec = int(e.headers.get("Retry-After", 5))
                logging.warning(f"Rate limited (429). Retrying in {wait_sec}s...")
                time.sleep(wait_sec)
                continue
            if e.code in (500, 502, 503, 504) and attempt < max_retries - 1:
                logging.warning(f"Server error {e.code}. Retrying in 2s...")
                time.sleep(2)
                continue
            raise RuntimeError(f"HTTP {e.code} on {method} {url}: {err_body}")
        except urllib.error.URLError as e:
            if attempt < max_retries - 1:
                time.sleep(2)
                continue
            raise RuntimeError(f"Network error on {method} {url}: {e.reason}")


def fetch_linear_issues(linear_token: str, issue_id: str = None) -> list:
    """Fetch issues from Linear via GraphQL."""
    headers = {
        "Authorization": linear_token,
        "Content-Type": "application/json",
    }
    if issue_id:
        query = """
        query($id: String!) {
          issue(id: $id) {
            id
            identifier
            title
            description
            url
            updatedAt
            priority
            priorityLabel
            state {
              name
              type
            }
            project {
              name
            }
            labels {
              nodes {
                id
                name
              }
            }
            documents {
              nodes {
                id
                title
                slugId
              }
            }
            comments(last: 10) {
              nodes {
                id
                body
                createdAt
                user {
                  name
                  isMe
                }
              }
            }
          }
        }
        """
        payload = {"query": query, "variables": {"id": issue_id}}
        res = api_request("https://api.linear.app/graphql", headers, payload, method="POST")
        issue = res.get("data", {}).get("issue")
        return [issue] if issue else []
    else:
        query = """
        query($teamId: String!) {
          team(id: $teamId) {
            issues(first: 100) {
              nodes {
                id
                identifier
                title
                description
                url
                updatedAt
                priority
                priorityLabel
                state {
                  name
                  type
                }
                project {
                  name
                }
                labels {
                  nodes {
                    id
                    name
                  }
                }
                documents {
                  nodes {
                    id
                    title
                    slugId
                  }
                }
                comments(last: 10) {
                  nodes {
                    id
                    body
                    createdAt
                    user {
                      name
                      isMe
                    }
                  }
                }
              }
            }
          }
        }
        """
        payload = {"query": query, "variables": {"teamId": LINEAR_TEAM_ID}}
        res = api_request("https://api.linear.app/graphql", headers, payload, method="POST")
        return res.get("data", {}).get("team", {}).get("issues", {}).get("nodes", [])


def fetch_existing_notion_pages(notion_token: str) -> dict:
    """Fetch all existing pages from Notion database and index by Linear ID."""
    headers = {
        "Authorization": f"Bearer {notion_token}",
        "Notion-Version": NOTION_API_VERSION,
        "Content-Type": "application/json",
    }
    url = f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query"
    pages_by_lid = {}
    has_more = True
    start_cursor = None

    while has_more:
        payload = {"page_size": 100}
        if start_cursor:
            payload["start_cursor"] = start_cursor

        res = api_request(url, headers, payload, method="POST")
        for page in res.get("results", []):
            props = page.get("properties", {})
            lid_prop = props.get("Linear ID", {}).get("rich_text", [])
            if lid_prop:
                lid_text = lid_prop[0].get("plain_text", "").strip()
                if lid_text:
                    pages_by_lid[lid_text] = page

        has_more = res.get("has_more", False)
        start_cursor = res.get("next_cursor")

    return pages_by_lid


def map_linear_to_notion_properties(issue: dict) -> dict:
    """Map a Linear issue dict to Notion property values."""
    title = issue.get("title", "")
    identifier = issue.get("identifier", "")
    url = issue.get("url", "")
    raw_status = (issue.get("state") or {}).get("name", "Backlog")
    status_name = STATUS_MAP.get(raw_status.lower(), raw_status)

    priority_int = issue.get("priority", 0)
    priority_name = PRIORITY_MAP.get(priority_int, "No priority")

    project_name = (issue.get("project") or {}).get("name")

    # Labels separation
    raw_labels = [l.get("name") for l in (issue.get("labels") or {}).get("nodes", [])]
    state_machine_val = None
    infra_type_val = None
    other_labels = []

    for lbl in raw_labels:
        if lbl in STATE_MACHINE_OPTIONS and state_machine_val is None:
            state_machine_val = lbl
        elif lbl in INFRASTRUCTURE_OPTIONS and infra_type_val is None:
            infra_type_val = lbl
        else:
            other_labels.append(lbl)

    now_iso = datetime.now(timezone.utc).isoformat()

    properties = {
        "Issues": {"title": [{"text": {"content": title}}]},
        "Linear ID": {"rich_text": [{"text": {"content": identifier}}]},
        "Linear URL": {"url": url},
        "Status": {"status": {"name": status_name}},
        "Priority": {"select": {"name": priority_name}},
        "Project": {"select": {"name": project_name}} if project_name else {"select": None},
        "State Machine": {"select": {"name": state_machine_val}} if state_machine_val else {"select": None},
        "Infrastructure Type": {"select": {"name": infra_type_val}} if infra_type_val else {"select": None},
        "Labels": {"multi_select": [{"name": l} for l in other_labels]},
        "Last Synced": {"date": {"start": now_iso}},
    }

    return properties


def extract_comparable_values(props: dict) -> dict:
    """Extract simple comparable values from Notion properties format."""
    title = props.get("Issues", {}).get("title", [{}])[0].get("text", {}).get("content", "")
    linear_id = props.get("Linear ID", {}).get("rich_text", [{}])[0].get("text", {}).get("content", "")
    linear_url = props.get("Linear URL", {}).get("url")
    status = (props.get("Status", {}).get("status") or {}).get("name")
    priority = (props.get("Priority", {}).get("select") or {}).get("name")
    project = (props.get("Project", {}).get("select") or {}).get("name")
    state_machine = (props.get("State Machine", {}).get("select") or {}).get("name")
    infra_type = (props.get("Infrastructure Type", {}).get("select") or {}).get("name")
    labels = sorted([l.get("name") for l in props.get("Labels", {}).get("multi_select", [])])

    return {
        "title": title,
        "linear_id": linear_id,
        "linear_url": linear_url,
        "status": status,
        "priority": priority,
        "project": project,
        "state_machine": state_machine,
        "infra_type": infra_type,
        "labels": labels,
    }


def extract_comparable_from_page(page: dict) -> dict:
    """Extract simple comparable values from an existing Notion page object."""
    props = page.get("properties", {})
    title_list = props.get("Issues", {}).get("title", [])
    title = title_list[0].get("plain_text", "") if title_list else ""

    lid_list = props.get("Linear ID", {}).get("rich_text", [])
    lid = lid_list[0].get("plain_text", "") if lid_list else ""

    url = props.get("Linear URL", {}).get("url")
    status = (props.get("Status", {}).get("status") or {}).get("name")
    priority = (props.get("Priority", {}).get("select") or {}).get("name")
    project = (props.get("Project", {}).get("select") or {}).get("name")
    state_machine = (props.get("State Machine", {}).get("select") or {}).get("name")
    infra_type = (props.get("Infrastructure Type", {}).get("select") or {}).get("name")
    labels = sorted([l.get("name") for l in props.get("Labels", {}).get("multi_select", [])])

    return {
        "title": title,
        "linear_id": lid,
        "linear_url": url,
        "status": status,
        "priority": priority,
        "project": project,
        "state_machine": state_machine,
        "infra_type": infra_type,
        "labels": labels,
    }


def format_page_markdown(issue: dict, trail_rows: list[dict] = None) -> str:
    """Format template markdown content for Notion page body."""
    identifier = issue.get("identifier", "")
    title = issue.get("title", "")
    url = issue.get("url", "")
    status = (issue.get("state") or {}).get("name", "Backlog")
    priority = issue.get("priorityLabel", "No priority")
    project = (issue.get("project") or {}).get("name", "N/A")
    description = (issue.get("description") or "").strip()

    # Determine State Machine
    raw_labels = [l.get("name") for l in (issue.get("labels") or {}).get("nodes", [])]
    state_machine_val = next((l for l in raw_labels if l in STATE_MACHINE_OPTIONS), "none")

    docs = (issue.get("documents") or {}).get("nodes", [])
    docs_section = ""
    if docs:
        docs_lines = ["\n## Attached Documents\n"]
        for d in docs:
            docs_lines.append(f"- **{d.get('title')}**")
        docs_section = "\n".join(docs_lines)

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    header = (
        f"> **Linear Issue**: [{identifier} — {title}]({url})  \n"
        f"> **Status**: {status} | **Priority**: {priority} | **Project**: {project}\n\n"
        f"---\n"
    )

    trail_table_header = (
        "## Agent Execution Trail\n\n"
        "| Phase | Engine & Model | Session ID | Worktree & Branch | Base Commit | Gate / Status | Log File | Updated |\n"
        "|---|---|---|---|---|---|---|---|\n"
    )

    if trail_rows:
        rows_str = ""
        for r in trail_rows:
            p = r.get("phase", state_machine_val)
            em = r.get("engine_model", "N/A")
            sid = r.get("session_id", "None")
            wt = r.get("worktree", "N/A")
            br = r.get("branch", "")
            wt_display = f"`{wt}` ({br})" if br and br != "N/A" else f"`{wt}`"
            base_sha = r.get("base_commit", "N/A")
            gate = r.get("gate", "none")
            log_p = r.get("log_file", "N/A")
            log_display = f"`{log_p}`" if log_p and log_p != "N/A" else "N/A"
            ts = r.get("updated_at", now_iso)
            rows_str += f"| `{p}` | `{em}` | `{sid}` | {wt_display} | `{base_sha}` | `{gate}` | {log_display} | {ts} |\n"
    else:
        rows_str = f"| `{state_machine_val}` | `None` | `None` | `N/A (Read-only)` | `N/A` | `none` | `N/A` | {now_iso} |\n"

    trail_block = f"{trail_table_header}{rows_str}\n---\n"

    body = description if description else "_No description provided in Linear._"
    notes_placeholder = "\n\n---\n\n## Notion Notes\n_Add private notes, links, or follow-up tasks here._\n"

    return f"{header}\n{trail_block}\n{body}\n{docs_section}{notes_placeholder}"


def update_page_content_via_ntn(page_id: str, content: str):
    """Update Notion page body content using ntn CLI markdown parser."""
    try:
        subprocess.run(
            ["ntn", "pages", "edit", page_id, "--content", content],
            capture_output=True,
            text=True,
            check=True,
        )
    except Exception as e:
        logging.warning(f"Failed to update page content for {page_id} via ntn: {e}")


def sync_issue(
    issue: dict,
    existing_page: dict,
    notion_token: str,
    dry_run: bool = False,
    force: bool = False,
    sync_content: bool = False,
) -> str:
    """Sync a single Linear issue to Notion."""
    identifier = issue["identifier"]
    new_props = map_linear_to_notion_properties(issue)
    headers = {
        "Authorization": f"Bearer {notion_token}",
        "Notion-Version": NOTION_API_VERSION,
        "Content-Type": "application/json",
    }

    if existing_page:
        page_id = existing_page["id"]
        old_vals = extract_comparable_from_page(existing_page)
        new_vals = extract_comparable_values(new_props)

        # Check diff
        diffs = []
        for k, v in new_vals.items():
            if old_vals.get(k) != v:
                diffs.append(f"{k}: '{old_vals.get(k)}' -> '{v}'")

        if not diffs and not force and not sync_content:
            logging.info(f"[{identifier}] No property changes detected. Skipping.")
            return "skipped"

        logging.info(f"[{identifier}] Updating page {page_id} (changes: {', '.join(diffs) if diffs else 'content/forced'})")
        if dry_run:
            return "updated (dry-run)"

        if diffs or force:
            url = f"https://api.notion.com/v1/pages/{page_id}"
            api_request(url, headers, {"properties": new_props}, method="PATCH")

        if sync_content:
            logging.info(f"[{identifier}] Syncing markdown description to page body...")
            content = format_page_markdown(issue)
            update_page_content_via_ntn(page_id, content)

        return "updated"
    else:
        logging.info(f"[{identifier}] Creating new page in Notion database...")
        if dry_run:
            return "created (dry-run)"

        url = "https://api.notion.com/v1/pages"
        payload = {
            "parent": {"database_id": NOTION_DATABASE_ID},
            "properties": new_props,
        }
        res = api_request(url, headers, payload, method="POST")
        new_page_id = res.get("id")
        logging.info(f"[{identifier}] Created page ID: {new_page_id}")

        # Populate page content with Linear description & template
        logging.info(f"[{identifier}] Populating initial markdown content & template...")
        content = format_page_markdown(issue)
        update_page_content_via_ntn(new_page_id, content)

        return "created"


def main():
    parser = argparse.ArgumentParser(description="Sync Linear issues to Notion")
    parser.add_argument("--issue", type=str, help="Sync specific issue identifier, e.g. TAI-78")
    parser.add_argument("--dry-run", action="store_true", help="Preview sync actions without mutating Notion")
    parser.add_argument("--force", action="store_true", help="Force update properties even if unchanged")
    parser.add_argument("--sync-content", action="store_true", help="Sync/overwrite page body with Linear description")
    parser.add_argument("--ignore-registry", action="store_true", help="Sync all projects ignoring projects.json")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose debug logging")
    args = parser.parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=log_level, format="%(asctime)s [%(levelname)s] %(message)s")

    linear_token = get_linear_token()
    notion_token = get_notion_token()

    registry = {} if args.ignore_registry else load_project_registry()
    if registry:
        logging.info(f"Loaded project registry with {len(registry)} active project(s): {list(registry.keys())}")
    else:
        logging.info("Project registry filter inactive or empty.")

    logging.info("Fetching existing pages from Notion...")
    existing_pages = fetch_existing_notion_pages(notion_token)
    logging.info(f"Found {len(existing_pages)} existing issue page(s) in Notion.")

    logging.info(f"Fetching Linear issues (filter: {args.issue or 'all team issues'})...")
    issues = fetch_linear_issues(linear_token, issue_id=args.issue)
    logging.info(f"Fetched {len(issues)} issue(s) from Linear.")

    # Filter by registered projects if registry active
    if registry:
        filtered_issues = []
        for iss in issues:
            proj_name = (iss.get("project") or {}).get("name")
            if proj_name in registry:
                filtered_issues.append(iss)
            else:
                logging.info(f"[{iss.get('identifier')}] Project '{proj_name}' is not in projects.json. Skipping.")
        issues = filtered_issues
        logging.info(f"Issues remaining after project filter: {len(issues)}")

    stats = {"created": 0, "updated": 0, "skipped": 0, "dry-run": 0}
    for iss in issues:
        ident = iss.get("identifier")
        existing_page = existing_pages.get(ident)
        res = sync_issue(
            iss,
            existing_page,
            notion_token,
            dry_run=args.dry_run,
            force=args.force,
            sync_content=args.sync_content,
        )
        if "created" in res:
            stats["created"] += 1
        elif "updated" in res:
            stats["updated"] += 1
        elif "skipped" in res:
            stats["skipped"] += 1

    logging.info(f"Sync complete. Stats: {stats}")


if __name__ == "__main__":
    main()
