#!/usr/bin/env python3
"""
context_harvester.py — Pre-trigger Context & Prior Art Harvester

Capabilities:
1. Runs Graphify queries against the target canonical repository to discover relevant
   architecture nodes, existing superpowers plans, and component relationships.
2. Cross-checks Linear for related or overlapping issues across the team.
3. Reads and extracts project architectural invariants from the target repository's AGENTS.md.
4. Compiles and persists raw evidence to assets/linear-notion-tracking/dossiers/<ident>_dossier.json.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import subprocess
from datetime import datetime, timezone

CURRENT_DIR = Path(__file__).resolve().parent
DOSSIERS_DIR = CURRENT_DIR / "dossiers"
DOSSIERS_DIR.mkdir(exist_ok=True)


def get_default_branch(canonical_path: str) -> str:
    """Determine the default branch (master or main) of the canonical repository."""
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


def scan_active_worktrees(canonical_path: str) -> list[dict]:
    """Scan all active git worktrees and identify checked out branches and modified files."""
    if not os.path.exists(canonical_path):
        return []

    default_branch = get_default_branch(canonical_path)
    canonical_real = os.path.realpath(canonical_path)

    try:
        res = subprocess.run(
            ["git", "-C", canonical_path, "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if res.returncode != 0:
            return []

        worktrees = []
        current_wt: dict = {}
        for line in res.stdout.splitlines():
            line = line.strip()
            if not line:
                if current_wt and os.path.realpath(current_wt.get("path", "")) != canonical_real:
                    worktrees.append(current_wt)
                current_wt = {}
                continue

            if line.startswith("worktree "):
                current_wt["path"] = line[9:]
            elif line.startswith("HEAD "):
                current_wt["head"] = line[5:]
            elif line.startswith("branch "):
                current_wt["branch"] = line[7:].replace("refs/heads/", "")

        if current_wt and os.path.realpath(current_wt.get("path", "")) != canonical_real:
            worktrees.append(current_wt)

        # For each external worktree, discover modified files against default branch
        for wt in worktrees:
            wt_path = wt.get("path", "")
            branch = wt.get("branch", "")
            modified_files = []
            modified_modules = set()

            if os.path.exists(wt_path):
                # 1. Diff against default branch
                diff_cmd = ["git", "-C", canonical_path, "diff", "--name-only", f"{default_branch}...{branch}"]
                d_res = subprocess.run(diff_cmd, capture_output=True, text=True, timeout=10)
                if d_res.returncode == 0:
                    for f in d_res.stdout.splitlines():
                        f = f.strip()
                        if f:
                            modified_files.append(f)
                            # Extract module name (e.g. Sources/RawViewCore/Reader -> RawViewCore/Reader)
                            parts = Path(f).parts
                            if len(parts) >= 3 and parts[0] in ("Sources", "Tests"):
                                modified_modules.add(f"{parts[1]}/{parts[2]}")
                            elif len(parts) >= 2:
                                modified_modules.add(parts[0])

            wt["modified_files"] = modified_files
            wt["modified_modules"] = sorted(list(modified_modules))

        return worktrees
    except Exception as e:
        logging.warning(f"Failed to scan worktrees: {e}")
        return []


def detect_worktree_conflicts(
    active_worktrees: list[dict],
    issue_title: str,
    target_keywords: list[str] | None = None,
) -> dict:
    """Detect if active worktrees touch areas or modules relevant to the new issue."""
    if not active_worktrees:
        return {"has_conflict": False, "conflicts": []}

    title_lower = issue_title.lower()
    keywords = set(target_keywords or [])
    keywords.update([w for w in title_lower.split() if len(w) > 3])

    conflicts = []
    for wt in active_worktrees:
        branch = wt.get("branch", "")
        mod_files = wt.get("modified_files", [])
        mod_modules = wt.get("modified_modules", [])

        overlapping_files = []
        for f in mod_files:
            f_lower = f.lower()
            if any(k in f_lower for k in keywords):
                overlapping_files.append(f)

        overlapping_modules = []
        for m in mod_modules:
            m_lower = m.lower()
            if any(k in m_lower for k in keywords):
                overlapping_modules.append(m)

        if overlapping_files or overlapping_modules:
            conflicts.append({
                "worktree_path": wt.get("path"),
                "branch": branch,
                "overlapping_modules": overlapping_modules[:5],
                "overlapping_files_sample": overlapping_files[:5],
                "total_overlapping_files": len(overlapping_files),
            })

    return {
        "has_conflict": len(conflicts) > 0,
        "conflicts": conflicts,
    }


def sweep_backlog_for_absorbable_ideas(
    team_issues: list[dict],
    current_issue: dict,
    canonical_path: str,
) -> list[dict]:
    """Find issues in Backlog or unstarted states that share the same component/module scope and should be merged."""
    curr_id = current_issue.get("identifier", "")
    curr_title = current_issue.get("title", "").lower()
    curr_tokens = {w for w in curr_title.split() if len(w) > 3}

    absorbable = []
    for iss in team_issues:
        ident = iss.get("identifier")
        if ident == curr_id:
            continue

        status = (iss.get("state") or {}).get("name", "")
        # Only absorb unstarted or backlog issues
        if status.lower() not in ("backlog", "triage", "todo"):
            continue

        title = iss.get("title", "")
        title_lower = title.lower()
        title_tokens = {w for w in title_lower.split() if len(w) > 3}
        common = curr_tokens.intersection(title_tokens)

        # Significant match: sharing 2+ keywords or 1 high-specificity domain term
        high_spec_terms = ["windows", "reader", "profile", "export", "keithley", "canvas", "tokens"]
        is_absorbable = len(common) >= 2 or any(t in common for t in high_spec_terms)

        if is_absorbable:
            absorbable.append({
                "id": iss.get("id"),
                "identifier": ident,
                "title": title,
                "status": status,
                "common_tokens": list(common),
                "url": iss.get("url"),
                "description": iss.get("description") or "",
            })

    return absorbable


def run_graphify_query(canonical_path: str, query_text: str) -> dict:
    """Run graphify query in canonical repo and extract nodes and edges."""
    if not os.path.exists(canonical_path):
        return {"nodes": [], "raw_output": "Canonical path does not exist."}

    graph_file = Path(canonical_path) / "graphify-out" / "graph.json"
    if not graph_file.exists():
        logging.info(f"No graphify-out/graph.json in {canonical_path}. Skipping Graphify traversal.")
        return {"nodes": [], "raw_output": "graphify-out/graph.json not present."}

    try:
        cmd = ["/Users/tai/.local/bin/graphify", "query", query_text]
        res = subprocess.run(
            cmd,
            cwd=canonical_path,
            capture_output=True,
            text=True,
            timeout=15,
        )
        output = res.stdout.strip()
        nodes = []
        for line in output.splitlines():
            line = line.strip()
            if line.startswith("NODE "):
                parts = line[5:].split(" [", 1)
                node_name = parts[0]
                metadata = parts[1].rstrip("]") if len(parts) > 1 else ""
                nodes.append({"name": node_name, "metadata": metadata})

        return {
            "query": query_text,
            "nodes_count": len(nodes),
            "nodes": nodes,
            "raw_output": output,
        }
    except Exception as e:
        logging.warning(f"Graphify query failed: {e}")
        return {"nodes": [], "error": str(e)}


def check_linear_overlaps(linear_issues: list[dict], current_issue: dict) -> list[dict]:
    """Find related or potentially overlapping issues in the same Linear team."""
    curr_id = current_issue.get("identifier", "")
    curr_title = current_issue.get("title", "").lower()
    curr_tokens = {w for w in curr_title.split() if len(w) > 3}

    overlaps = []
    for iss in linear_issues:
        ident = iss.get("identifier")
        if ident == curr_id:
            continue

        title = iss.get("title", "")
        title_tokens = {w for w in title.lower().split() if len(w) > 3}
        common = curr_tokens.intersection(title_tokens)

        # Consider an overlap if common significant keywords exist
        if len(common) >= 2 or (len(common) >= 1 and any(k in common for k in ["windows", "folder", "reader", "workspace", "detection"])):
            overlaps.append({
                "identifier": ident,
                "title": title,
                "status": (iss.get("state") or {}).get("name", "Unknown"),
                "common_keywords": list(common),
                "url": iss.get("url"),
            })

    return overlaps


def extract_repo_invariants(canonical_path: str) -> list[str]:
    """Extract architectural invariants and constraints from project AGENTS.md."""
    agents_file = Path(canonical_path) / "AGENTS.md"
    if not agents_file.exists():
        return []

    invariants = []
    try:
        text = agents_file.read_text(encoding="utf-8")
        in_constraints_section = False
        for line in text.splitlines():
            line_s = line.strip()
            if any(h in line_s.lower() for h in ["constraint", "invariant", "philosophy", "rules"]):
                in_constraints_section = True
                continue
            if in_constraints_section:
                if line_s.startswith("#"):
                    in_constraints_section = False
                elif line_s.startswith(("-", "*", "1.", "2.", "3.", "4.", "5.")):
                    invariants.append(line_s.lstrip("-*0123456789. "))
    except Exception as e:
        logging.warning(f"Failed to read invariants from {agents_file}: {e}")

    return invariants[:10]  # Cap at top 10 constraints


def harvest_dossier(
    current_issue: dict,
    project_config: dict,
    team_issues: list[dict],
) -> dict:
    """Harvest comprehensive pre-flight context and compile a raw JSON dossier."""
    ident = current_issue.get("identifier", "UNKNOWN")
    title = current_issue.get("title", "")
    canonical_path = project_config.get("canonical_path", "")

    logging.info(f"[{ident}] Harvesting pre-flight context for: '{title}'...")

    # 1. Graphify Knowledge Traversal
    query_keywords = f"{title}"
    graphify_data = run_graphify_query(canonical_path, query_keywords)

    # 2. Linear Overlap & Backlog Absorption Cross-check
    overlaps = check_linear_overlaps(team_issues, current_issue)
    absorbable = sweep_backlog_for_absorbable_ideas(team_issues, current_issue, canonical_path)

    # 3. Active Git Worktrees & Conflict Scan
    active_worktrees = scan_active_worktrees(canonical_path)
    worktree_conflict_data = detect_worktree_conflicts(active_worktrees, title)

    # 4. Repository Invariants
    invariants = extract_repo_invariants(canonical_path)

    # 5. Compile Dossier
    dossier = {
        "identifier": ident,
        "title": title,
        "harvested_at": datetime.now(timezone.utc).isoformat(),
        "canonical_path": canonical_path,
        "default_branch": get_default_branch(canonical_path),
        "target_project": project_config.get("linear_name"),
        "graphify_evidence": graphify_data,
        "linear_overlaps": overlaps,
        "absorbable_ideas": absorbable,
        "active_worktrees": [
            {
                "path": wt.get("path"),
                "branch": wt.get("branch"),
                "modified_modules": wt.get("modified_modules"),
                "total_modified_files": len(wt.get("modified_files", [])),
            }
            for wt in active_worktrees
        ],
        "worktree_conflicts": worktree_conflict_data,
        "repository_invariants": invariants,
        "issue_metadata": {
            "status": (current_issue.get("state") or {}).get("name"),
            "priority": current_issue.get("priorityLabel"),
            "description_length": len(current_issue.get("description") or ""),
        },
    }

    # Save to disk
    dossier_file = DOSSIERS_DIR / f"{ident.lower()}_dossier.json"
    with open(dossier_file, "w", encoding="utf-8") as f:
        json.dump(dossier, f, indent=2)

    logging.info(f"[{ident}] Saved raw context dossier to {dossier_file} (Found {len(graphify_data.get('nodes', []))} graph nodes, {len(overlaps)} Linear overlap(s)).")
    return dossier


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    test_issue = {"identifier": "TAI-78", "title": "Idea 1 — Windows launch and distribution"}
    test_proj = {
        "linear_name": "Raw Viewer",
        "canonical_path": "/Users/tai/research-projects/tools/raw-viewer",
    }
    d = harvest_dossier(test_issue, test_proj, [])
    print(f"Test dossier compiled: {d['identifier']} | Graph nodes: {len(d['graphify_evidence']['nodes'])}")
