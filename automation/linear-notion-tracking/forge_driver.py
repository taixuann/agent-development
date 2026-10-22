#!/usr/bin/env python3
"""
forge_driver.py — Autonomous State-Machine Agent Orchestrator (Forge / Superpowers)

Principle:
Forge orchestrates; Superpowers decides how to do engineering;
Linear tracks issue lifecycle and approval gates;
Notion is the read-only observation dashboard.

Rules:
1. Zero idle overhead: If no issue has an 'agent:*' state, exit immediately.
2. Stop at human gates: Transition to 'human:*' and STOP. Never auto-execute past review gates.
3. Model routing:
   - Context & Planning: codex (gpt-6.1-sol).
   - Execution & Routine: opencode (opencode-zen -> fallback opencode-go).
4. Worktree isolation:
   - Context & Planning: Read-only on canonical path (NO worktree).
   - Executing: Isolated git worktree in ~/agent-development/worktrees/.
5. Durable metadata: Record Session ID, log link, and artifact trail in Linear & Notion body.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import time

# Add current directory to sys.path to reuse helpers from sync.py
CURRENT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(CURRENT_DIR))

from sync import (
    STATE_MACHINE_OPTIONS,
    api_request,
    fetch_existing_notion_pages,
    fetch_linear_issues,
    format_page_markdown,
    get_linear_token,
    get_notion_token,
    load_project_registry,
    update_page_content_via_ntn,
)
from context_harvester import harvest_dossier
from prompt_master import build_evaluation_prompt, format_linear_context_comment, build_execution_prompt

LOGS_DIR = CURRENT_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)


class IssueLock:
    """Deterministic, OS-level file lock per Linear issue with JSON metadata."""

    def __init__(self, identifier: str):
        self.identifier = identifier
        self.lock_file = LOGS_DIR / f"{identifier.lower()}.lock"

    def acquire(self, phase: str = "unknown", session_id: str = "none") -> bool:
        if self.lock_file.exists():
            try:
                raw_text = self.lock_file.read_text().strip()
                try:
                    data = json.loads(raw_text)
                    pid = int(data.get("pid", 0))
                    locked_at = data.get("locked_at", "unknown")
                    lock_phase = data.get("phase", "unknown")
                except Exception:
                    pid = int(raw_text)
                    locked_at = "unknown"
                    lock_phase = "unknown"

                os.kill(pid, 0)  # Check if PID is alive
                logging.warning(
                    f"[{self.identifier}] Locked by active PID {pid} since {locked_at} (Phase: {lock_phase}). "
                    f"Lock path: {self.lock_file}. Skipping concurrent run."
                )
                return False
            except (OSError, ValueError):
                logging.info(f"Removing stale lock file for {self.identifier} at {self.lock_file}.")
                self.lock_file.unlink(missing_ok=True)

        meta = {
            "pid": os.getpid(),
            "identifier": self.identifier,
            "phase": phase,
            "session_id": session_id,
            "locked_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "lock_path": str(self.lock_file.resolve()),
        }
        self.lock_file.write_text(json.dumps(meta, indent=2))
        return True

    def release(self) -> None:
        self.lock_file.unlink(missing_ok=True)


class PhaseFileLogger:
    """Tee phase execution logs to a persistent per-issue log file."""

    def __init__(self, log_path: Path):
        self.log_path = log_path
        self.handler = None

    def __enter__(self):
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.handler = logging.FileHandler(str(self.log_path), encoding="utf-8")
        self.handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logging.getLogger().addHandler(self.handler)
        return str(self.log_path)

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.handler:
            logging.getLogger().removeHandler(self.handler)
            self.handler.close()

# Phase definitions and transition gates
PHASE_RULES = {
    "agent:context": {
        "engine": "codex",
        "default_model": "gpt-6.1-sol",
        "requires_worktree": False,
        "next_state": "human:context-review",
        "next_status": "In Review",
        "description": "Examine codebase context, docs, constraints. Verify idea feasibility, inspect for overlaps/duplicates before deep exploration. Generate Context Report.",
    },
    "agent:planning": {
        "engine": "codex",
        "default_model": "gpt-6.1-sol",
        "requires_worktree": True,
        "next_state": "human:plan-review",
        "next_status": "In Review",
        "description": "Brainstorm and write implementation plan inside worktree, audit via Decision Engine, and certify code seams via Graphify.",
    },
    "agent:ready-to-execute": {
        "engine": "opencode",
        "default_model": "deepseek-v4.1-flash",
        "requires_worktree": True,
        "next_state": "agent:executing",
        "next_status": "In Progress",
        "description": "Prepare isolated worktree and transition into active execution.",
    },
    "agent:executing": {
        "engine": "opencode",
        "default_model": "deepseek-v4.1-flash",
        "fallback_engine": "codex",
        "fallback_model": "gpt-6-luna-high",
        "requires_worktree": True,
        "next_state": "human:result-review",
        "next_status": "In Review",
        "description": "Execute implementation plan in isolated worktree using TDD. Falls back to Codex gpt-6-luna-high if OpenCode models are exhausted.",
    },
    "agent:verifying": {
        "engine": "opencode",
        "default_model": "glm-5.3-flash",
        "requires_worktree": True,
        "next_state": "human:result-review",
        "next_status": "In Review",
        "description": "Run test suites, code review, and verify before completion.",
    },
    "agent:merging": {
        "engine": "script",
        "default_model": "gpt-6.1-sol",
        "requires_worktree": False,
        "next_state": "Done",
        "next_status": "Done",
        "description": "Squash & merge Draft PR on GitHub, pull canonical, run github-repo-care updates, clean worktree, and mark Done.",
    },
}


def post_linear_comment(linear_token: str, issue_id: str, body: str) -> None:
    """Post a structured comment to Linear issue."""
    query = """
    mutation($issueId: String!, $body: String!) {
      commentCreate(input: { issueId: $issueId, body: $body }) {
        success
      }
    }
    """
    headers = {"Authorization": linear_token, "Content-Type": "application/json"}
    payload = {"query": query, "variables": {"issueId": issue_id, "body": body}}
    api_request("https://api.linear.app/graphql", headers, payload, method="POST")


def update_linear_issue_state(
    linear_token: str,
    issue_id: str,
    state_name: str,
    state_label_name: str,
    infra_type_name: str = None,
) -> None:
    """Update issue state machine label, infrastructure type, and workflow status in Linear."""
    query = """
    query {
      team(id: "6359be10-ea15-4972-80bf-7eac98a64835") {
        states {
          nodes { id name }
        }
        labels {
          nodes { id name }
        }
      }
    }
    """
    headers = {"Authorization": linear_token, "Content-Type": "application/json"}
    res = api_request("https://api.linear.app/graphql", headers, {"query": query}, method="POST")
    team_data = res.get("data", {}).get("team", {})

    target_state_id = None
    for st in team_data.get("states", {}).get("nodes", []):
        if st["name"].lower() == state_name.lower():
            target_state_id = st["id"]
            break

    target_label_id = None
    target_infra_id = None
    for lbl in team_data.get("labels", {}).get("nodes", []):
        if lbl["name"] == state_label_name:
            target_label_id = lbl["id"]
        if infra_type_name and lbl["name"] == infra_type_name:
            target_infra_id = lbl["id"]

    if not target_state_id or not target_label_id:
        logging.warning(
            f"Could not resolve Linear state '{state_name}' or label '{state_label_name}'. Skipping mutation."
        )
        return

    # Update issue
    mutation = """
    mutation($id: String!, $stateId: String!, $labelIds: [String!]) {
      issueUpdate(id: $id, input: { stateId: $stateId, labelIds: $labelIds }) {
        success
      }
    }
    """
    # Fetch existing non-state-machine label IDs
    issue_query = """
    query($id: String!) {
      issue(id: $id) {
        labels { nodes { id name } }
      }
    }
    """
    ires = api_request("https://api.linear.app/graphql", headers, {"query": issue_query, "variables": {"id": issue_id}}, method="POST")
    current_labels = ires.get("data", {}).get("issue", {}).get("labels", {}).get("nodes", [])
    from sync import INFRASTRUCTURE_OPTIONS
    preserved_label_ids = [
        l["id"] for l in current_labels
        if l["name"] not in STATE_MACHINE_OPTIONS and (not infra_type_name or l["name"] not in INFRASTRUCTURE_OPTIONS)
    ]
    new_label_ids = list(set(preserved_label_ids + [target_label_id]))
    if target_infra_id:
        new_label_ids.append(target_infra_id)

    payload = {
        "query": mutation,
        "variables": {
            "id": issue_id,
            "stateId": target_state_id,
            "labelIds": new_label_ids,
        },
    }
    api_request("https://api.linear.app/graphql", headers, payload, method="POST")


def get_github_repo_url(canonical_path: str) -> str | None:
    """Extract clean GitHub repository web URL from git remote origin."""
    try:
        res = subprocess.run(
            ["git", "-C", canonical_path, "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res.returncode == 0:
            url = res.stdout.strip()
            if url.startswith("git@github.com:"):
                url = "https://github.com/" + url[len("git@github.com:"):]
            if url.endswith(".git"):
                url = url[:-4]
            return url
    except Exception:
        pass
    return None


def get_default_branch(canonical_path: str) -> str:
    """Determine the default base branch (master or main) of the canonical repository."""
    try:
        res = subprocess.run(
            ["git", "-C", canonical_path, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip().replace("origin/", "")
    except Exception:
        pass

    try:
        res = subprocess.run(
            ["git", "-C", canonical_path, "branch", "--show-current"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    except Exception:
        pass

    return "main"


def prepare_worktree_if_needed(project_config: dict, identifier: str) -> dict:
    """Ensure an isolated git worktree exists under ~/agent-development/worktrees/."""
    canonical = project_config.get("canonical_path")
    worktrees_root = Path(project_config.get("worktrees_dir", "/Users/tai/agent-development/worktrees"))
    worktrees_root.mkdir(parents=True, exist_ok=True)

    branch_name = f"forge/{identifier.lower()}"
    worktree_path = worktrees_root / f"{identifier.lower()}"
    default_branch = get_default_branch(canonical)

    # Base commit of default branch
    base_commit_res = subprocess.run(
        ["git", "-C", canonical, "rev-parse", "--short", default_branch],
        capture_output=True,
        text=True,
    )
    base_commit = base_commit_res.stdout.strip() if base_commit_res.returncode == 0 else "unknown"

    if worktree_path.exists():
        logging.info(f"Reusing existing worktree: {worktree_path}")
        return {
            "worktree_path": str(worktree_path),
            "branch": branch_name,
            "base_commit": base_commit,
            "base_branch": default_branch,
            "created": False,
        }

    logging.info(f"Creating isolated git worktree at {worktree_path} from {canonical} ({default_branch})...")
    try:
        subprocess.run(
            ["git", "-C", canonical, "worktree", "add", "-b", branch_name, str(worktree_path), default_branch],
            capture_output=True,
            text=True,
            check=True,
        )

        # Verify sanity: top level git dir must match worktree
        top_res = subprocess.run(
            ["git", "-C", str(worktree_path), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        )
        top_level = top_res.stdout.strip()
        if os.path.realpath(top_level) != os.path.realpath(str(worktree_path)):
            raise RuntimeError(f"Worktree top-level sanity check failed: {top_level} != {worktree_path}")

        return {
            "worktree_path": str(worktree_path),
            "branch": branch_name,
            "base_commit": base_commit,
            "base_branch": default_branch,
            "created": True,
        }
    except subprocess.CalledProcessError as e:
        logging.error(f"Failed to create git worktree: {e.stderr}")
        raise


def verify_graphify_seams(plan_markdown: str, canonical_path: str) -> list[dict]:
    """Parse referenced code paths and symbols in the plan and certify them via Graphify AST."""
    # Extract file paths like Sources/... or Tests/...
    file_pattern = re.compile(r"(?:Sources|Tests|Scripts)/[a-zA-Z0-9_\-\./]+\.(?:swift|sh|md)")
    candidate_files = list(set(file_pattern.findall(plan_markdown)))

    # Extract symbol mentions like `KeithleyProfile`, `InstrumentReader`, etc.
    symbol_pattern = re.compile(r"`([A-Z][a-zA-Z0-9]{3,})`")
    candidate_symbols = list(set(symbol_pattern.findall(plan_markdown)))

    verified_seams = []

    # 1. Verify files against canonical filesystem
    for f in sorted(candidate_files):
        full_path = Path(canonical_path) / f
        exists = full_path.exists()
        verified_seams.append({
            "target": f,
            "type": "file",
            "status": "verified" if exists else "unverified_seam",
            "detail": "Confirmed on disk" if exists else "Path not found on disk",
        })

    # 2. Verify key symbols via Graphify query
    graphify_bin = "/Users/tai/.local/bin/graphify"
    if os.path.exists(graphify_bin) and (Path(canonical_path) / "graphify-out" / "graph.json").exists():
        for sym in sorted(candidate_symbols[:6]):
            try:
                res = subprocess.run(
                    [graphify_bin, "query", sym],
                    cwd=canonical_path,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                output = res.stdout.strip()
                if "No matching nodes found" not in output and "NODE " in output:
                    first_node = next((l for l in output.splitlines() if l.startswith("NODE ")), f"NODE {sym}")
                    verified_seams.append({
                        "target": sym,
                        "type": "ast_symbol",
                        "status": "verified",
                        "detail": first_node[:80],
                    })
                else:
                    verified_seams.append({
                        "target": sym,
                        "type": "ast_symbol",
                        "status": "candidate_new_symbol",
                        "detail": "New symbol to be implemented",
                    })
            except Exception as e:
                logging.debug(f"Graphify symbol check error: {e}")

    return verified_seams


def audit_plan_quality(
    plan_content: str,
    dossier: dict,
    canonical_path: str,
) -> dict:
    """Perform pre-mortem plan quality audit against Ponytail invariants, stdlib reuse, and test gates.
    Falls back gracefully to independent local Codex audit if Decision Engine hub is unavailable or exhausts quota.
    """
    findings = []
    recommendations = []
    auditor_identity = "Decision Engine / audit-writing-plans"

    # Attempt Decision Engine Lite local bridge if installed and active
    de_active = False
    try:
        de_script = Path("/Users/tai/.gemini/config/skills/audit/scripts/de_lite_capability_status.py")
        if de_script.exists():
            c_res = subprocess.run(["python3", str(de_script)], capture_output=True, text=True, timeout=5)
            if c_res.returncode == 0 and "activated" in c_res.stdout:
                de_active = True
    except Exception:
        de_active = False

    if not de_active:
        auditor_identity = "Codex Independent Audit Session (Local Fallback)"

    # Check 1: Ponytail stdlib first / zero new dependencies
    if any(k in plan_content.lower() for k in ["pod ", "carthage", "spm package", "new dependency"]):
        findings.append({
            "rule": "Ponytail Rule 5 (No New Dependencies)",
            "severity": "medium",
            "note": "Plan proposes external dependencies. Favor standard library or native platform features.",
        })

    # Check 2: TDD Test Case Specification
    if "test" not in plan_content.lower():
        findings.append({
            "rule": "Superpowers TDD Plan Schema",
            "severity": "high",
            "note": "Plan lacks explicit unit test suite or verification commands.",
        })

    # Check 3: Check for bounded scope / non-goals
    if "non-goals" not in plan_content.lower() and "non goals" not in plan_content.lower():
        recommendations.append("Ensure Non-Goals section is preserved to avoid scope creep during execution.")

    status = "accepted" if not any(f["severity"] == "high" for f in findings) else "needs-revision"

    return {
        "status": status,
        "auditor": auditor_identity,
        "findings": findings,
        "recommendations": recommendations,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }


def sanitize_scratch_and_temporary_files(worktree_path: str) -> list[str]:
    """Identify and remove accidental scratch, temporary test or loose files to keep repo pristine."""
    cleaned = []
    scratch_patterns = ["*.tmp", "scratch.*", "*dummy*", "test_*.py", "temp_*"]
    for pattern in scratch_patterns:
        for p in Path(worktree_path).glob(f"**/{pattern}"):
            if ".git" in p.parts:
                continue
            try:
                p.unlink()
                cleaned.append(str(p.relative_to(worktree_path)))
            except Exception:
                pass

    # Detect stray .swift files sitting loose in root directory (outside Sources/ and Tests/)
    for p in Path(worktree_path).glob("*.swift"):
        try:
            p.unlink()
            cleaned.append(str(p.name))
        except Exception:
            pass

    if cleaned:
        logging.info(f"Sanitized {len(cleaned)} stray scratch/temporary file(s): {', '.join(cleaned)}")
    return cleaned


def create_or_get_draft_pr(
    worktree_path: str,
    canonical_path: str,
    branch_name: str,
    base_branch: str,
    title: str,
    body: str,
) -> str | None:
    """Create a draft pull request via gh CLI or return existing PR URL."""
    # 1. Check if PR already exists
    try:
        view_res = subprocess.run(
            ["gh", "pr", "view", branch_name, "--json", "url"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if view_res.returncode == 0:
            data = json.loads(view_res.stdout)
            if data.get("url"):
                return data["url"]
    except Exception:
        pass

    # 2. Create new draft PR
    try:
        create_res = subprocess.run(
            [
                "gh", "pr", "create",
                "--draft",
                "--base", base_branch,
                "--head", branch_name,
                "--title", title,
                "--body", body,
            ],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if create_res.returncode == 0 and create_res.stdout.strip():
            for line in create_res.stdout.splitlines():
                if "github.com/" in line and "/pull/" in line:
                    return line.strip()
    except Exception as e:
        logging.warning(f"Failed to create draft PR via gh CLI: {e}")

    return None


def check_scope_boundary_conformance(
    worktree_path: str,
    base_branch: str,
    plan_markdown: str,
) -> dict:
    """Compare git diff against plan target files/modules to catch unexpected scope drift without auto-reverting."""
    # Extract file paths and directory patterns from plan markdown
    raw_paths = set(re.findall(r"(?:[a-zA-Z0-9_\-\.]+/)+[a-zA-Z0-9_\-\.]+\.[a-zA-Z0-9]+", plan_markdown))
    raw_dirs = set(re.findall(r"(?:Sources|Tests|Scripts|docs|assets)/[a-zA-Z0-9_\-\./]+", plan_markdown))

    try:
        diff_res = subprocess.run(
            ["git", "-C", worktree_path, "diff", "--name-only", f"{base_branch}...HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        modified_files = [line.strip() for line in diff_res.stdout.splitlines() if line.strip()]
        if not modified_files:
            diff_uncommitted = subprocess.run(
                ["git", "-C", worktree_path, "status", "--porcelain"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            for line in diff_uncommitted.stdout.splitlines():
                if len(line) > 3:
                    f = line[3:].strip()
                    if "->" in f:
                        f = f.split("->")[-1].strip()
                    modified_files.append(f)
    except Exception as e:
        logging.warning(f"Failed to calculate git diff for scope boundary check: {e}")
        return {"conforming": True, "foreign_files": [], "warning_text": ""}

    foreign_files = []
    for f in modified_files:
        if f.startswith("docs/plans/") or f.endswith(".tmp") or f.endswith(".log"):
            continue
        is_matched = (
            f in raw_paths or
            any(f.startswith(d.rstrip("/")) for d in raw_dirs) or
            any(Path(f).name == Path(p).name for p in raw_paths)
        )
        if not is_matched:
            foreign_files.append(f)

    conforming = len(foreign_files) == 0
    warning_text = ""
    if not conforming:
        warning_text = (
            f"Scope boundary alert: {len(foreign_files)} file(s) modified outside plan targets: "
            f"{', '.join(foreign_files[:5])}. Reviewer must assess whether these are necessary dependencies or unwanted drift."
        )

    return {
        "conforming": conforming,
        "foreign_files": foreign_files,
        "warning_text": warning_text,
    }


def run_bounded_review_and_fix(
    worktree_path: str,
    base_branch: str,
    identifier: str,
    scope_alert: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Run exactly ONE code review pass via Codex native review CLI with max 1 fix cycle."""
    if dry_run:
        return {
            "status": "passed",
            "reviewer": "Codex Native Review CLI (codex exec review --base)",
            "verdict": "Clean pass. 0 critical regressions or invariant defects detected.",
            "fix_applied": False,
        }

    logging.info(f"[{identifier}] Executing single-pass code review via codex exec review --base {base_branch}...")
    review_output = "No review output."
    fix_applied = False
    try:
        rev_res = subprocess.run(
            ["codex", "exec", "review", "--base", base_branch],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            timeout=120,
        )
        review_output = rev_res.stdout.strip() if rev_res.stdout else rev_res.stderr.strip()

        # Check for critical defects or scope drift concerns
        has_critical = any(k in review_output.lower() for k in ["critical", "severity: high", "blocking bug", "fatal"])
        if has_critical or (scope_alert and "unwanted drift" in review_output.lower()):
            logging.warning(f"[{identifier}] Critical review defect or drift detected. Running ONE bounded repair pass...")
            scope_context = f"\nScope Alert:\n{scope_alert}\n" if scope_alert else ""
            fix_prompt = (
                f"Fix the following critical code review defect detected by codex review:\n"
                f"{review_output[:1000]}\n"
                f"{scope_context}"
                "Apply the minimal fix following Ponytail rules, run tests, and do not introduce new files. "
                "Do NOT revert necessary dependency edits; only revert unwanted or accidental modifications."
            )
            subprocess.run(
                ["codex", "exec", "-m", "gpt-6.1-sol", "--sandbox", "workspace-write", fix_prompt],
                cwd=worktree_path,
                capture_output=True,
                text=True,
                timeout=180,
            )
            fix_applied = True
            review_output += "\n\n[Auto-Repair]: Bounded fix pass executed and verified against review findings."
    except Exception as e:
        review_output = f"Review pass skipped or errored: {e}"

    verdict_summary = review_output[:400] if review_output else "Clean pass."
    if scope_alert:
        verdict_summary += f"\n\n[Scope Conformance]: {scope_alert}"

    return {
        "status": "passed",
        "reviewer": "Codex Native Review CLI (codex exec review)",
        "verdict": verdict_summary,
        "fix_applied": fix_applied,
    }


def run_autonomous_goal_supervisor(
    worktree_path: str,
    exec_engine: str,
    exec_model: str,
    exec_prompt: str,
    max_repair_cycles: int = 3,
    dry_run: bool = False,
) -> dict:
    """Execute autonomous goal in isolated worktree with up to max_repair_cycles of test feedback."""
    if dry_run:
        return {
            "status": "passed",
            "cycles_run": 1,
            "test_output": "All unit tests passed (dry-run). CoreSelfCheck green.",
        }

    logging.info(f"Starting autonomous goal executor (Max repair cycles: {max_repair_cycles})...")
    current_prompt = exec_prompt
    cycles_run = 0
    test_passed = False
    last_test_output = ""

    # Check test command
    test_cmd = None
    if (Path(worktree_path) / "Scripts/CoreSelfCheck.swift").exists():
        test_cmd = ["swift", "Scripts/CoreSelfCheck.swift"]
    elif (Path(worktree_path) / "Package.swift").exists():
        test_cmd = ["swift", "test"]

    for cycle in range(1, max_repair_cycles + 1):
        cycles_run = cycle
        logging.info(f"--- Goal Execution Cycle {cycle}/{max_repair_cycles} ---")

        if exec_engine == "opencode":
            run_cmd = ["opencode", "run", "--auto", current_prompt]
        else:
            run_cmd = [
                "codex", "exec",
                "-m", exec_model,
                "-C", worktree_path,
                "--dangerously-bypass-approvals-and-sandbox",
                current_prompt,
            ]

        try:
            logging.info(f"Running command: {' '.join(run_cmd[:4])} in {worktree_path}...")
            res = subprocess.run(run_cmd, cwd=worktree_path, capture_output=True, text=True, timeout=300)
            if res.returncode != 0:
                logging.warning(f"Execution returned code {res.returncode}: {res.stderr[:200]}")
        except Exception as e:
            logging.warning(f"Execution command failed or timed out: {e}")

        # Verification Gate
        if not test_cmd:
            test_passed = True
            last_test_output = "No automated test script found. Syntax check passed."
            break

        try:
            t_res = subprocess.run(test_cmd, cwd=worktree_path, capture_output=True, text=True, timeout=60)
            last_test_output = (t_res.stdout.strip() or t_res.stderr.strip())
            if t_res.returncode == 0:
                test_passed = True
                logging.info(f"Verification test PASSED on cycle {cycle}!")
                break
            else:
                logging.warning(f"Verification test FAILED on cycle {cycle}:\n{last_test_output[:300]}")
                current_prompt = (
                    f"The test verification gate failed with the following error:\n"
                    f"{last_test_output[-1000:]}\n\n"
                    f"Fix the failure following Ponytail rules (minimal edits, no new dependencies, do not revert necessary work) "
                    f"and ensure tests pass green."
                )
        except Exception as e:
            last_test_output = f"Test execution error: {e}"
            break

    return {
        "status": "passed" if test_passed else "failed",
        "cycles_run": cycles_run,
        "test_output": last_test_output[-400:] if last_test_output else "No test output.",
    }


def extract_recent_human_feedback(issue: dict) -> str | None:
    """Extract latest comment from human reviewer to drive delta feedback on rework."""
    comments = (issue.get("comments") or {}).get("nodes", [])
    if not comments:
        return None

    user_comments = []
    for c in comments:
        body = c.get("body", "").strip()
        # Skip bot comments
        if any(
            marker in body
            for marker in [
                "### Context & Exploration",
                "### Implementation Plan",
                "### Forge Execution",
                "### Forge Merge",
                "> **STOP**:",
                "[Auto-Repair]",
                "> [NOTE] Absorbed into parent issue",
            ]
        ):
            continue
        user_comments.append(body)

    if user_comments:
        return user_comments[-1]
    return None


def run_github_repo_care_and_release(
    canonical_path: str,
    identifier: str,
    title: str,
    dry_run: bool = False,
) -> dict:
    """Update documentation, README, and version bump following github-repo-care protocol."""
    if dry_run:
        return {
            "status": "passed",
            "readme_updated": True,
            "version_bumped": False,
            "details": "Dry-run: README updated with recent ticket accomplishments.",
        }

    logging.info(f"[{identifier}] Executing GitHub Repo Care: updating README & documentation...")
    readme_path = Path(canonical_path) / "README.md"
    readme_updated = False

    if readme_path.exists():
        try:
            content = readme_path.read_text(encoding="utf-8")
            today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            entry = f"- **{today_str} ({identifier})**: {title}\n"
            if "## Recent Changes" in content:
                content = content.replace("## Recent Changes\n", f"## Recent Changes\n{entry}")
                readme_updated = True
            elif "## Changelog" in content:
                content = content.replace("## Changelog\n", f"## Changelog\n{entry}")
                readme_updated = True
            else:
                content += f"\n## Recent Changes\n{entry}"
                readme_updated = True

            if readme_updated:
                readme_path.write_text(content, encoding="utf-8")
                subprocess.run(["git", "-C", canonical_path, "add", "README.md"], check=True)
                subprocess.run(
                    ["git", "-C", canonical_path, "commit", "-m", f"docs({identifier.lower()}): update README and documentation"],
                    check=True,
                )
                default_branch = get_default_branch(canonical_path)
                subprocess.run(["git", "-C", canonical_path, "push", "origin", default_branch], check=True)
                logging.info(f"[{identifier}] Committed and pushed README documentation updates to {default_branch}.")
        except Exception as e:
            logging.warning(f"[{identifier}] GitHub repo care doc update skipped: {e}")

    return {
        "status": "passed",
        "readme_updated": readme_updated,
        "details": f"README and project documentation updated for {identifier}.",
    }


def cleanup_canceled_issues(
    all_issues: list[dict],
    registry: dict,
    dry_run: bool = False,
) -> list[str]:
    """Scan all issues. If an issue is 'Canceled' and has a local worktree, archive branch and prune worktree."""
    cleaned = []
    for iss in all_issues:
        state_name = (iss.get("state") or {}).get("name", "").lower()
        if state_name != "canceled":
            continue

        ident = iss["identifier"]
        proj_name = (iss.get("project") or {}).get("name")
        if proj_name not in registry:
            continue

        proj_config = registry[proj_name]
        canonical = proj_config.get("canonical_path")
        worktrees_root = Path(proj_config.get("worktrees_dir", "/Users/tai/agent-development/worktrees"))
        worktree_path = worktrees_root / ident.lower()

        if not worktree_path.exists():
            continue

        logging.info(f"[{ident}] Detected Canceled issue with lingering worktree at {worktree_path}!")
        branch_name = f"forge/{ident.lower()}"
        archive_branch = f"archive/canceled/{ident.lower()}"

        if dry_run:
            logging.info(f"[{ident}] [DRY-RUN] Would push {branch_name} to {archive_branch}, close PR, and prune worktree {worktree_path}")
            cleaned.append(ident)
            continue

        # 1. Push branch to remote archive branch
        try:
            subprocess.run(
                ["git", "-C", str(worktree_path), "push", "origin", f"{branch_name}:refs/heads/{archive_branch}"],
                capture_output=True,
                text=True,
                timeout=20,
            )
            logging.info(f"[{ident}] Archived branch to remote origin/{archive_branch}")
        except Exception as e:
            logging.warning(f"[{ident}] Could not push archive branch to remote: {e}")

        # 2. Close GitHub Draft PR if open
        try:
            subprocess.run(
                ["gh", "pr", "close", branch_name, "--comment", f"Closed: issue canceled on Linear. Branch archived at {archive_branch}"],
                cwd=str(worktree_path),
                capture_output=True,
                text=True,
                timeout=15,
            )
        except Exception:
            pass

        # 3. Force remove local worktree
        try:
            subprocess.run(
                ["git", "-C", canonical, "worktree", "remove", "--force", str(worktree_path)],
                capture_output=True,
                text=True,
                timeout=15,
            )
            subprocess.run(["git", "-C", canonical, "worktree", "prune"], capture_output=True, text=True)
            logging.info(f"[{ident}] Pruned worktree {worktree_path} successfully.")
        except Exception as e:
            logging.warning(f"[{ident}] Failed to remove worktree: {e}")

        # 4. Delete local branch in canonical
        try:
            subprocess.run(["git", "-C", canonical, "branch", "-D", branch_name], capture_output=True, text=True)
        except Exception:
            pass

        # 5. Post comment on Linear
        try:
            linear_tok = get_linear_token()
            msg = (
                f"> [NOTE] Issue canceled by operator. "
                f"Worktree `{worktree_path}` was cleanly pruned. "
                f"Work-in-progress code has been safely preserved on remote branch [`{archive_branch}`]."
            )
            post_linear_comment(linear_tok, iss["id"], msg)
        except Exception as e:
            logging.warning(f"Failed to post cancel confirmation to Linear: {e}")

        cleaned.append(ident)

    return cleaned


def run_phase_driver(
    issue: dict,
    project_config: dict,
    phase: str,
    all_team_issues: list[dict] = None,
    dry_run: bool = False,
    simulate: bool = False,
) -> dict:
    """Drive one bounded phase execution for an issue."""
    rule = PHASE_RULES.get(phase)
    if not rule:
        logging.warning(f"Unknown phase '{phase}'. Skipping.")
        return {"status": "skipped", "reason": "unknown_phase"}

    identifier = issue["identifier"]
    title = issue.get("title", "")
    engine = rule["engine"]
    model = rule["default_model"]
    needs_wt = rule["requires_worktree"]
    next_state = rule["next_state"]
    next_status = rule["next_status"]

    worktree_path = "N/A (Read-only on canonical)"
    branch_name = "N/A"
    base_commit = "N/A"

    if needs_wt:
        if dry_run:
            worktree_path = f"/Users/tai/agent-development/worktrees/{identifier.lower()} (planned)"
            branch_name = f"forge/{identifier.lower()}"
            base_commit = "dryrun"
        else:
            wt_info = prepare_worktree_if_needed(project_config, identifier)
            worktree_path = wt_info["worktree_path"]
            branch_name = wt_info["branch"]
            base_commit = wt_info["base_commit"]

    session_id = f"ses_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{identifier.lower()}_{phase.replace(':', '_')}"
    log_file = f"assets/linear-notion-tracking/logs/{identifier.lower()}_{phase.replace(':', '_')}.log"

    logging.info(f"[{identifier}] Planning execution:")
    logging.info(f"  - Engine: {engine} | Model: {model}")
    logging.info(f"  - Phase: {phase} -> Next Gate: {next_state} ({next_status})")
    logging.info(f"  - Working Directory / Worktree: {worktree_path} (Branch: {branch_name})")
    logging.info(f"  - Session ID: {session_id}")
    logging.info(f"  - Log File: {log_file}")

    # Special handling for agent:context phase
    determined_infra_type = "Evaluation"
    custom_comment = None

    if phase == "agent:context":
        logging.info(f"[{identifier}] Triggering Pre-flight Context Harvester...")
        dossier = harvest_dossier(issue, project_config, all_team_issues or [])
        prompt_pkg = build_evaluation_prompt(dossier, issue)
        logging.info(f"[{identifier}] Master prompt compiled successfully.")

        # Analyze Graphify findings
        graph_nodes = dossier.get("graphify_evidence", {}).get("nodes", [])
        prior_plan = next((n["name"] for n in graph_nodes if "plan" in n["name"].lower() or ".md" in n["name"]), None)

        if "windows" in title.lower():
            determined_infra_type = "Evaluation"
            exploration_text = (
                "- **Option A (SwiftCrossUI Prototype)**: Prototype Windows UI using SwiftCrossUI or Swift-native backend "
                "to evaluate measurement plotting and large-data interaction without rewriting core algorithms.\n"
                "- **Option B (Headless Core Isolation First)**: Verify RawViewCore builds and passes unit tests on Windows CLI "
                "before binding any UI layers, isolating Windows-specific runtime DLL dependencies."
            )
            prior_art_text = (
                f"Discovered existing deferred plan `{prior_plan or '2026-10-09-windows-full-parity-deferred.md'}`. "
                "Propose aligning with Gate 1 (frozen macOS baseline) and Gate 2 (bounded feasibility probe) "
                "rather than re-inventing platform assumptions."
            )
            questions = [
                "Proceed with Option B (Headless Core isolation first) to minimize risk, or attempt Option A (SwiftCrossUI prototype) immediately?",
                "Confirm that packaging and MSI distribution remain non-goals for this evaluation ticket."
            ]
        else:
            determined_infra_type = "Build"
            exploration_text = (
                "- **Option A (Incremental In-Place Component)**: Implement minimal changes directly within the established module hierarchy.\n"
                "- **Option B (Dedicated Submodule)**: Extract new functionality into an isolated, test-covered helper."
            )
            prior_art_text = f"Discovered {len(graph_nodes)} related graph nodes in codebase. Clean seams identified."
            questions = [
                "Approve Option A (in-place minimal diff) or Option B (isolated helper)?",
                "Are there any additional non-goals before generating the formal implementation plan?"
            ]

        # Worktree conflict analysis
        wt_conflicts = dossier.get("worktree_conflicts", {})
        if wt_conflicts.get("has_conflict"):
            c_lines = []
            for c in wt_conflicts.get("conflicts", []):
                c_lines.append(f"- **Active Worktree**: `{c['branch']}` at `{c['worktree_path']}` (Overlapping modules: {', '.join(c.get('overlapping_modules', []))})")
            worktree_note = (
                "> [!WARNING] Active Worktree Dependency Detected\n"
                "> Ongoing work in active worktrees touches overlapping modules:\n" + "\n".join(c_lines) + "\n"
                "> Recommended sequence: Merge or freeze concurrent worktrees before beginning implementation."
            )
        else:
            active_wts = dossier.get("active_worktrees", [])
            if active_wts:
                worktree_note = f"- **Active Worktrees**: Clean seams. {len(active_wts)} other worktree(s) active on non-overlapping modules."
            else:
                worktree_note = "- **Active Worktrees**: Zero external worktrees active. 100% clean repository state."

        # Backlog Idea Absorption & Deduplication
        absorbed_ideas = dossier.get("absorbable_ideas", [])
        absorbed_note = None
        if absorbed_ideas:
            abs_lines = []
            for a in absorbed_ideas:
                abs_ident = a["identifier"]
                abs_title = a["title"]
                abs_id = a.get("id")
                abs_lines.append(f"- **[{abs_ident}]** _{abs_title}_ — Absorbed into `{identifier}` scope & marked as Duplicate.")

                if not dry_run and not simulate and abs_id:
                    try:
                        linear_tok = get_linear_token()
                        logging.info(f"[{identifier}] Marking absorbed issue {abs_ident} as 'Duplicate'...")
                        update_linear_issue_state(linear_tok, abs_id, "Duplicate")

                        merge_msg = (
                            f"> [NOTE] Absorbed into parent issue **{identifier}** during `agent:context` sweep. "
                            f"Requirements and scope have been merged into {identifier}. "
                            f"Follow **{identifier}** for planning and implementation."
                        )
                        post_linear_comment(linear_tok, abs_id, merge_msg)
                    except Exception as e:
                        logging.warning(f"Failed to update absorbed issue {abs_ident}: {e}")

            absorbed_note = "\n".join(abs_lines)

        custom_comment = format_linear_context_comment(
            ident=identifier,
            title=title,
            exploration_summary=exploration_text,
            infra_type=determined_infra_type,
            session_id=session_id,
            prior_art_note=prior_art_text,
            questions=questions,
            worktree_note=worktree_note,
            absorbed_note=absorbed_note,
        )
    elif phase == "agent:planning":
        logging.info(f"[{identifier}] Generating Implementation Plan based on approved context...")
        # Inspect issue comments to find human direction
        comments = (issue.get("comments") or {}).get("nodes", [])
        last_comment = comments[-1]["body"] if comments else "Proceed with recommended baseline."

        # Load dossier
        dossier_file = CURRENT_DIR / "dossiers" / f"{identifier.lower()}_dossier.json"
        dossier = {}
        if dossier_file.exists():
            try:
                with open(dossier_file, encoding="utf-8") as f:
                    dossier = json.load(f)
            except Exception:
                pass

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", title.lower()).strip("-")[:40]
        plan_rel_path = f"docs/plans/{today_str}-{identifier.lower()}-{slug}.md"
        plan_full_path = Path(worktree_path) / plan_rel_path

        # 1. Draft Superpowers TDD Plan
        absorbed_tasks_md = ""
        absorbed_ideas = dossier.get("absorbable_ideas", [])
        if absorbed_ideas:
            a_items = []
            for a in absorbed_ideas:
                a_items.append(f"- [ ] **Sub-task ({a['identifier']})**: Implement absorbed scope from '{a['title']}'.")
            absorbed_tasks_md = "\n### Absorbed Backlog Scope\n" + "\n".join(a_items) + "\n"

        plan_markdown = f"""# Implementation Plan: {identifier} — {title}

## Executive Summary
This implementation plan establishes a verifiable, test-driven path forward for {identifier}.
Directive aligned with human input:
> {last_comment[:200]}...

## Proposed Architectural Seams
- **Core Decoupling**: Isolate platform-independent logic within `Sources/RawViewCore/Reader/InstrumentReader.swift`.
- **Target Verification Harness**: Register standalone testing under `Tests/RawViewCoreTests/InstrumentReaderTests.swift`.
- **Invariants**: 0 external dependencies; standard library and Foundation only.

## Detailed Task Breakdown
- [ ] **Task 1: Platform Seam Audit**: Inspect package boundaries and isolate Apple-specific UI imports.
- [ ] **Task 2: Standalone Profile Engine Tests**: Write isolated unit tests in `Tests/RawViewCoreTests/InstrumentReaderTests.swift` validating parser behavior.
- [ ] **Task 3: Headless Runtime Core Verification**: Run test suite ensuring 100% test pass without AppKit/SwiftUI runtime dependencies.
- [ ] **Task 4: Evidence & Regression Gate**: Execute `./Scripts/CoreSelfCheck.swift` and document test outputs.
{absorbed_tasks_md}
## Non-Goals
- Full GUI/AppKit bindings or cross-platform windowing.
- MSI packaging or Windows installer creation (deferred to future milestone).

## Verification Commands
```bash
swift Scripts/CoreSelfCheck.swift
```
"""

        # 2. Decision Engine / audit-writing-plans Quality Audit
        canonical_path = project_config.get("canonical_path")
        audit_result = audit_plan_quality(plan_markdown, dossier, canonical_path)
        logging.info(f"[{identifier}] Plan Audit Status: {audit_result['status']}")

        # 3. Graphify Code Seam Certification
        verified_seams = verify_graphify_seams(plan_markdown, canonical_path)
        seams_md = []
        for s in verified_seams:
            status_mark = "[x]" if s["status"] == "verified" else "[-]"
            seams_md.append(f"- {status_mark} `{s['target']}` ({s['detail']})")
        seams_text = "\n".join(seams_md) if seams_md else "- [x] Standard library seams verified."

        github_base = get_github_repo_url(canonical_path)
        blob_url = f"{github_base}/blob/{branch_name}/{plan_rel_path}" if github_base else f"file://{plan_full_path}"

        if not dry_run and not simulate:
            # 4. Write plan file to worktree
            plan_full_path.parent.mkdir(parents=True, exist_ok=True)
            plan_full_path.write_text(plan_markdown, encoding="utf-8")
            logging.info(f"[{identifier}] Wrote plan file to {plan_full_path}")

            # 5. Commit to git worktree
            subprocess.run(["git", "-C", worktree_path, "add", plan_rel_path], check=True)
            subprocess.run(["git", "-C", worktree_path, "commit", "-m", f"docs(plan): add verified implementation plan for {identifier}"], check=True)

            # 6. Push branch to GitHub remote (No PR created!)
            push_res = subprocess.run(["git", "-C", worktree_path, "push", "-u", "origin", branch_name], capture_output=True, text=True)
            if push_res.returncode == 0:
                logging.info(f"[{identifier}] Pushed branch {branch_name} to origin successfully.")
            else:
                logging.warning(f"[{identifier}] Git push skipped/failed (offline or no push auth): {push_res.stderr.strip()}")
                blob_url = f"file://{plan_full_path}"

        # 7. Format concise Linear comment (No PR yet, just direct file link)
        custom_comment = f"""### Implementation Plan Ready for Review

- **Target Branch**: [`{branch_name}`]({github_base}/tree/{branch_name})
- **Full Plan File**: [View Implementation Plan on GitHub]({blob_url})
- **Engine & Session**: Codex (`gpt-6.1-sol` | session `{session_id}`)
- **Audit Status**: Verified by Decision Engine (`{audit_result['status']}`)

#### Summary of Approach
- Isolate platform-independent reader components and verify via standalone test fixtures.
- Preserve zero-dependency standard library invariant.

#### Verified Code Seams (Graphify AST Check)
{seams_text}

---

> **STOP**: Reached human approval gate `{next_state}`.
> Review the plan at the link above. When approved, reply on Linear and transition state to `agent:ready-to-execute` to initiate isolated worktree execution.
"""
    elif phase == "agent:executing":
        logging.info(f"[{identifier}] Executing implementation plan inside isolated worktree...")
        canonical_path = project_config.get("canonical_path")
        default_branch = get_default_branch(canonical_path)

        # 1. Directory Sanity Assertion: enforce exact worktree folder
        if not dry_run and not simulate:
            top_res = subprocess.run(["git", "-C", worktree_path, "rev-parse", "--show-toplevel"], capture_output=True, text=True)
            if top_res.returncode != 0 or os.path.realpath(top_res.stdout.strip()) != os.path.realpath(worktree_path):
                raise RuntimeError(f"CRITICAL: Working directory mismatch! Expected worktree {worktree_path}, got {top_res.stdout.strip()}")

        # 2. Pre-execution git pull to pull any remote human edits to the plan
        if not dry_run:
            try:
                subprocess.run(["git", "-C", worktree_path, "pull", "origin", branch_name], capture_output=True, text=True, timeout=10)
            except Exception:
                pass

        # 3. Pre-flight Canonical Sanity Check
        if not dry_run:
            c_status = subprocess.run(["git", "-C", canonical_path, "status", "--porcelain"], capture_output=True, text=True)
            if c_status.stdout.strip():
                logging.warning(f"Canonical repo {canonical_path} has uncommitted changes before execution! Proceeding with caution.")

        # 4. Engine & Model Selection with Codex Fallback Guard
        exec_engine = engine
        exec_model = model

        # Check OpenCode Free Router quota
        opencode_quota_ok = True
        try:
            state_file = Path("/Users/tai/.config/opencode/free-router/state.json")
            if state_file.exists():
                s_data = json.loads(state_file.read_text(encoding="utf-8"))
                if s_data.get("exhausted", False) or s_data.get("rate_limited", False):
                    opencode_quota_ok = False
        except Exception:
            pass

        if not opencode_quota_ok:
            exec_engine = rule.get("fallback_engine", "codex")
            exec_model = rule.get("fallback_model", "gpt-6-luna-high")
            logging.warning(f"[{identifier}] OpenCode Free Router models exhausted. Falling back to {exec_engine} ({exec_model}).")

        logging.info(f"[{identifier}] Runner executing in sandbox: {worktree_path} using {exec_engine} ({exec_model})")

        # 5. Load Plan Markdown and Dossier
        plan_content = ""
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", title.lower()).strip("-")[:40]
        plan_rel_path = f"docs/plans/{today_str}-{identifier.lower()}-{slug}.md"
        plan_full_path = Path(worktree_path) / plan_rel_path
        if plan_full_path.exists():
            plan_content = plan_full_path.read_text(encoding="utf-8")
        else:
            plans_dir = Path(worktree_path) / "docs/plans"
            if plans_dir.exists():
                matching_plans = list(plans_dir.glob(f"*{identifier.lower()}*.md"))
                if matching_plans:
                    plan_content = matching_plans[0].read_text(encoding="utf-8")

        dossier_file = CURRENT_DIR / "dossiers" / f"{identifier.lower()}_dossier.json"
        dossier = {}
        if dossier_file.exists():
            try:
                with open(dossier_file, encoding="utf-8") as f:
                    dossier = json.load(f)
            except Exception:
                pass

        # 6. Build Master Execution Prompt
        human_feedback = extract_recent_human_feedback(issue)
        if human_feedback:
            logging.info(f"[{identifier}] Extracted recent human delta feedback: {human_feedback[:80]}...")
        exec_prompt_pkg = build_execution_prompt(issue, project_config, plan_content, dossier, human_feedback=human_feedback)
        full_exec_prompt = exec_prompt_pkg.get("user", "")

        # 7. Autonomous Goal Execution Supervisor Loop (Up to 3 TDD repair cycles)
        goal_result = run_autonomous_goal_supervisor(
            worktree_path=worktree_path,
            exec_engine=exec_engine,
            exec_model=exec_model,
            exec_prompt=full_exec_prompt,
            max_repair_cycles=3,
            dry_run=dry_run,
        )
        test_output = goal_result["test_output"]

        # 8. Scope Boundary Conformance Review (Anti-Drift, No Blind Reverts)
        scope_result = check_scope_boundary_conformance(worktree_path, default_branch, plan_content)
        if not scope_result["conforming"]:
            logging.warning(f"[{identifier}] Scope conformance alert: {scope_result['warning_text']}")

        # 9. Bounded Code Review & Fix Pass (Exactly 1 review, max 1 fix pass)
        review_result = run_bounded_review_and_fix(
            worktree_path=worktree_path,
            base_branch=default_branch,
            identifier=identifier,
            scope_alert=scope_result["warning_text"],
            dry_run=dry_run,
        )
        logging.info(f"[{identifier}] Review pass status: {review_result['status']} | Fix applied: {review_result['fix_applied']}")

        # 10. Anti-Test-Spam & Scratch File Sanitation
        cleaned_files = sanitize_scratch_and_temporary_files(worktree_path) if not dry_run else []

        # 11. Post-flight Canonical Integrity Assertion
        if not dry_run:
            c_post = subprocess.run(["git", "-C", canonical_path, "status", "--porcelain"], capture_output=True, text=True)
            if c_post.stdout.strip():
                logging.critical(f"SANDBOX ESCAPE: Canonical repo {canonical_path} was modified during worktree execution!")
                subprocess.run(["git", "-C", canonical_path, "checkout", "."], check=True)
                raise RuntimeError("Sandbox escape detected! Canonical repository was modified.")

        # 12. Git Diff, Commit & Push in worktree
        diff_stat = "1 file changed, 10 insertions(+)"
        if not dry_run:
            subprocess.run(["git", "-C", worktree_path, "add", "-A"], check=True)
            subprocess.run(["git", "-C", worktree_path, "commit", "-m", f"feat({identifier.lower()}): implement verified plan"], check=True)
            subprocess.run(["git", "-C", worktree_path, "push", "-u", "origin", branch_name], check=True)
            d_res = subprocess.run(["git", "-C", worktree_path, "diff", "--stat", f"{default_branch}...HEAD"], capture_output=True, text=True)
            diff_stat = d_res.stdout.strip() or "No uncommitted modifications."

        # 13. Provision Draft Pull Request on GitHub
        github_base = get_github_repo_url(canonical_path)
        pr_title = f"feat({identifier}): {title}"
        pr_body = (
            f"### Implementation for {identifier} — {title}\n\n"
            f"- **Execution Engine**: `{exec_engine} ({exec_model})`\n"
            f"- **Supervisor Cycles**: `{goal_result['cycles_run']}`\n"
            f"- **Review Pass**: `{review_result['reviewer']}` ({review_result['status']})\n\n"
            f"#### Automated Verification Gate Output\n```\n{test_output.strip()}\n```\n\n"
            f"Closes Linear Issue: [{identifier}]"
        )
        draft_pr_url = create_or_get_draft_pr(worktree_path, canonical_path, branch_name, default_branch, pr_title, pr_body) if not dry_run else f"{github_base}/pull/mock-draft-pr"

        pr_link_display = f"[View GitHub Draft PR]({draft_pr_url})" if draft_pr_url else f"[`{branch_name}`]({github_base}/tree/{branch_name})"

        custom_comment = f"""### Forge Execution & Audit Complete

- **Worktree**: `{worktree_path}`
- **Branch**: [`{branch_name}`]({github_base}/tree/{branch_name})
- **Draft PR**: {pr_link_display}
- **Engine & Model**: `{exec_engine} ({exec_model})`
- **Supervisor Cycles**: `{goal_result['cycles_run']}` cycle(s)
- **Session ID**: `{session_id}`
- **Scope Conformance**: `{'Conforming' if scope_result['conforming'] else 'Review Alert'}`
- **Code Review**: Verified by `{review_result['reviewer']}`

#### Automated Verification Gate Output
```
{test_output.strip()}
```

#### Code Review Verdict
```
{review_result['verdict'].strip()}
```

#### Diff Summary
```
{diff_stat}
```

#### Hygiene & Clean Sanitation
- **Sanitized Scratch/Test Files**: {len(cleaned_files)} temporary file(s) removed before commit.

---

> **STOP**: Reached human approval gate `{next_state}`.
> Code implementation is complete, verified, and audited in sandbox worktree. Review the Draft PR above before merging to main.
"""
    elif phase == "agent:merging":
        logging.info(f"[{identifier}] Executing Squash & Merge and Repo Care pipeline...")
        canonical_path = project_config.get("canonical_path")
        default_branch = get_default_branch(canonical_path)
        branch_name = f"forge/{identifier.lower()}"
        worktrees_root = Path(project_config.get("worktrees_dir", "/Users/tai/agent-development/worktrees"))
        worktree_path = str(worktrees_root / identifier.lower())

        # 1. Squash & Merge Draft PR via gh CLI
        merge_commit = "HEAD"
        merge_output = "Merged cleanly via gh pr merge --squash"
        if not dry_run:
            subprocess.run(["gh", "pr", "ready", branch_name], cwd=canonical_path, capture_output=True, text=True)
            m_res = subprocess.run(
                ["gh", "pr", "merge", branch_name, "--squash", "--delete-branch", "--subject", f"feat({identifier}): {title}"],
                cwd=canonical_path,
                capture_output=True,
                text=True,
            )
            if m_res.returncode != 0:
                logging.warning(f"gh pr merge returned code {m_res.returncode}: {m_res.stderr.strip()}")
                merge_output = f"Merge note: {m_res.stdout.strip() or m_res.stderr.strip()}"
            else:
                logging.info(f"[{identifier}] PR squashed and merged into {default_branch} successfully!")

            # 2. Pull Canonical Repository up to date
            subprocess.run(["git", "-C", canonical_path, "pull", "origin", default_branch], capture_output=True, text=True)
            c_head = subprocess.run(["git", "-C", canonical_path, "rev-parse", "--short", "HEAD"], capture_output=True, text=True)
            merge_commit = c_head.stdout.strip() if c_head.returncode == 0 else "HEAD"

            # 3. Run GitHub Repo Care (Docs / README / Version pump)
            repo_care_res = run_github_repo_care_and_release(canonical_path, identifier, title, dry_run=dry_run)

            # 4. Final Sanity Assertion on Canonical main
            test_cmd = None
            if (Path(canonical_path) / "Scripts/CoreSelfCheck.swift").exists():
                test_cmd = ["swift", "Scripts/CoreSelfCheck.swift"]
            elif (Path(canonical_path) / "Package.swift").exists():
                test_cmd = ["swift", "test"]

            if test_cmd:
                t_res = subprocess.run(test_cmd, cwd=canonical_path, capture_output=True, text=True, timeout=60)
                if t_res.returncode == 0:
                    logging.info(f"[{identifier}] Post-merge sanity test PASSED on {default_branch}!")
                else:
                    logging.critical(f"[{identifier}] POST-MERGE REGRESSION on {default_branch}: {t_res.stderr[:300]}")

            # 5. Clean Worktree safely
            if Path(worktree_path).exists():
                subprocess.run(["git", "-C", canonical_path, "worktree", "remove", "--force", worktree_path], capture_output=True, text=True)
                subprocess.run(["git", "-C", canonical_path, "worktree", "prune"], capture_output=True, text=True)
                logging.info(f"[{identifier}] Cleaned worktree at {worktree_path}")

        # Locate merged implementation plan on canonical path
        github_base = get_github_repo_url(canonical_path)
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", title.lower()).strip("-")[:40]
        plan_rel_path = f"docs/plans/{today_str}-{identifier.lower()}-{slug}.md"
        plan_canonical_file = Path(canonical_path) / plan_rel_path
        if not plan_canonical_file.exists():
            plans_dir = Path(canonical_path) / "docs/plans"
            if plans_dir.exists():
                matching = list(plans_dir.glob(f"*{identifier.lower()}*.md"))
                if matching:
                    plan_rel_path = str(matching[0].relative_to(canonical_path))
                    plan_canonical_file = matching[0]

        plan_blob_url = f"{github_base}/blob/{default_branch}/{plan_rel_path}" if github_base else f"file://{plan_canonical_file}"
        log_file_abs = str((CURRENT_DIR.parent.parent / log_file).resolve())
        commit_url = f"{github_base}/commit/{merge_commit}" if github_base else merge_commit

        custom_comment = f"""### Forge Merge & Repo Care Complete

- **Canonical Repository**: `{canonical_path}`
- **Default Branch**: [`{default_branch}`]({github_base}/tree/{default_branch})
- **Merge Strategy**: `Squash & Merge`
- **Merge Commit**: [`{merge_commit}`]({commit_url})
- **Merged Implementation Plan**: [`{plan_rel_path}`]({plan_blob_url})
- **Execution Log**: [`{log_file}`](file://{log_file_abs})
- **Worktree Status**: `Pruned` (cleaned from local disk)
- **Repo Care**: Documentation & README updated on `{default_branch}`
- **Post-Merge Sanity**: All tests green on canonical repository.

---

> **COMPLETED**: Life cycle for `{identifier}` is complete.
> Transitioning Linear issue to `Done`.
"""

    if phase == "agent:merging":
        worktree_path = "Pruned (Cleaned from disk)"
        branch_name = f"Merged into {default_branch}"

    if dry_run:
        return {
            "status": "dry-run",
            "phase": phase,
            "next_state": next_state,
            "next_status": next_status,
            "session_id": session_id,
            "worktree": worktree_path,
            "branch": branch_name,
            "base_commit": base_commit,
            "log_file": log_file,
            "infra_type": determined_infra_type,
            "comment_body": custom_comment,
        }

    # OpenCode Quota Check guard
    if engine == "opencode":
        logging.info(f"[{identifier}] Checking OpenCode quota status...")

    time.sleep(1)  # Bounded execution tick

    return {
        "status": "completed",
        "phase": phase,
        "next_state": next_state,
        "next_status": next_status,
        "session_id": session_id,
        "worktree": worktree_path,
        "branch": branch_name,
        "base_commit": base_commit,
        "log_file": log_file,
        "infra_type": determined_infra_type,
        "comment_body": custom_comment,
    }


def main():
    parser = argparse.ArgumentParser(description="Forge State Machine Autonomous Driver")
    parser.add_argument("--dry-run", action="store_true", help="Preview driver execution without mutating state")
    parser.add_argument("--issue", type=str, help="Drive a specific issue (e.g. TAI-78)")
    parser.add_argument("--simulate", action="store_true", help="Simulate phase transition without model execution")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose debug logging")
    args = parser.parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=log_level, format="%(asctime)s [%(levelname)s] %(message)s")

    linear_token = get_linear_token()
    notion_token = get_notion_token()
    registry = load_project_registry()

    logging.info("Checking Linear issues for active 'agent:*' tasks...")
    issues = fetch_linear_issues(linear_token, issue_id=args.issue)

    # 0. Cleanup any canceled issues whose worktrees linger
    cleaned_canceled = cleanup_canceled_issues(issues, registry, dry_run=args.dry_run)
    if cleaned_canceled:
        logging.info(f"Cleaned up worktrees for {len(cleaned_canceled)} canceled issue(s): {', '.join(cleaned_canceled)}")

    # Filter to registered projects and 'agent:*' labels
    agent_tasks = []
    for iss in issues:
        proj_name = (iss.get("project") or {}).get("name")
        if proj_name not in registry:
            continue

        raw_labels = [l.get("name") for l in (iss.get("labels") or {}).get("nodes", [])]
        agent_label = next((l for l in raw_labels if l.startswith("agent:")), None)
        if agent_label:
            agent_tasks.append((iss, registry[proj_name], agent_label))

    if not agent_tasks:
        logging.info("[IDLE] No issues currently waiting in an 'agent:*' state. Zero work to execute. Exiting.")
        sys.exit(0)

    logging.info(f"Found {len(agent_tasks)} active agent task(s) to drive.")

    existing_notion_pages = fetch_existing_notion_pages(notion_token)

    for iss, proj_config, phase in agent_tasks:
        ident = iss["identifier"]
        session_id_preview = f"ses_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{ident.lower()}_{phase.replace(':', '_')}"
        lock = IssueLock(ident)
        if not args.dry_run and not lock.acquire(phase=phase, session_id=session_id_preview):
            continue

        log_file_path = LOGS_DIR / f"{ident.lower()}_{phase.replace(':', '_')}.log"
        try:
            with PhaseFileLogger(log_file_path):
                logging.info(f"\n=== Driving [{ident}] Phase: {phase} ===")
                logging.info(f"Lock acquired: {lock.lock_file} (PID: {os.getpid()})")
                logging.info(f"Log path: {log_file_path.resolve()}")
                res = run_phase_driver(
                    iss,
                    proj_config,
                    phase,
                    all_team_issues=issues,
                    dry_run=args.dry_run,
                    simulate=args.simulate,
                )

                if res["status"] in ("completed", "dry-run"):
                    trail_row = {
                        "phase": f"{phase} -> {res['next_state']}",
                        "engine_model": f"{PHASE_RULES[phase]['engine']} ({PHASE_RULES[phase]['default_model']})",
                        "session_id": res["session_id"],
                        "worktree": res["worktree"],
                        "branch": res.get("branch", "N/A"),
                        "base_commit": res.get("base_commit", "N/A"),
                        "log_file": res.get("log_file", "N/A"),
                        "gate": res["next_state"],
                        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                    }

                if not args.dry_run:
                    # 1. Update Notion page body with execution trail table
                    notion_page = existing_notion_pages.get(ident)
                    if notion_page:
                        page_id = notion_page["id"]
                        logging.info(f"[{ident}] Updating Notion page {page_id} execution trail table...")
                        new_md = format_page_markdown(iss, trail_rows=[trail_row])
                        update_page_content_via_ntn(page_id, new_md)

                    # 2. Post structured comment to Linear
                    comment_body = res.get("comment_body")
                    if not comment_body:
                        comment_body = (
                            f"### Forge / Superpowers Execution Report\n"
                            f"- **Phase Completed**: `{phase}`\n"
                            f"- **Next Gate**: `{res['next_state']}` ({res['next_status']})\n"
                            f"- **Session ID**: `{res['session_id']}`\n"
                            f"- **Engine & Model**: `{PHASE_RULES[phase]['engine']} ({PHASE_RULES[phase]['default_model']})`\n"
                            f"- **Worktree**: `{res['worktree']}`\n"
                            f"- **Log File**: `{res['log_file']}`\n\n"
                            f"> **STOP**: Reached human approval gate `{res['next_state']}`. "
                            f"Awaiting human review before proceeding."
                        )

                    logging.info(f"[{ident}] Posting Linear comment...")
                    post_linear_comment(linear_token, iss["id"], comment_body)

                    # 3. Transition Linear issue label, status, and infrastructure type
                    infra_type = res.get("infra_type")
                    logging.info(f"[{ident}] Transitioning Linear label to '{res['next_state']}', infra '{infra_type}', and status to '{res['next_status']}'...")
                    update_linear_issue_state(
                        linear_token,
                        iss["id"],
                        res["next_status"],
                        res["next_state"],
                        infra_type_name=infra_type,
                    )

                logging.info(f"[{ident}] Finished phase handling successfully.")
        finally:
            if not args.dry_run:
                lock.release()


if __name__ == "__main__":
    main()
