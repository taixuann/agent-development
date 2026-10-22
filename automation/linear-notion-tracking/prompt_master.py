#!/usr/bin/env python3
"""
prompt_master.py — Standardized Evaluation & Brainstorming Prompt Generator

Capabilities:
1. Compiles the harvested dossier and issue details into a standardized, unbiased
   evaluation prompt for Codex (gpt-6.1-sol).
2. Directs Codex to explore design alternatives openly without stifling creativity.
3. Formats the full, rich Context & Exploration Summary for direct posting to Linear.
"""

from __future__ import annotations

import json
from pathlib import Path


def build_evaluation_prompt(dossier: dict, current_issue: dict) -> dict:
    """Build standardized master prompt for Codex evaluation."""
    ident = current_issue.get("identifier", "UNKNOWN")
    title = current_issue.get("title", "")
    description = (current_issue.get("description") or "").strip()
    project_name = dossier.get("target_project", "Unknown Project")
    canonical_path = dossier.get("canonical_path", "")

    # Extract Graphify highlights
    graph_nodes = dossier.get("graphify_evidence", {}).get("nodes", [])
    graph_summary = []
    for n in graph_nodes[:15]:
        graph_summary.append(f"- {n['name']} ({n.get('metadata', '')})")
    graph_text = "\n".join(graph_summary) if graph_summary else "No existing graph nodes found."

    # Extract Linear overlaps
    overlaps = dossier.get("linear_overlaps", [])
    overlap_summary = []
    for o in overlaps[:5]:
        overlap_summary.append(f"- [{o['identifier']}] {o['title']} (Status: {o['status']})")
    overlap_text = "\n".join(overlap_summary) if overlap_summary else "No overlapping Linear issues detected."

    # Invariants
    invariants = dossier.get("repository_invariants", [])
    invariants_text = "\n".join([f"- {inv}" for inv in invariants]) if invariants else "Standard clean architecture rules apply."

    system_prompt = (
        f"You are the Lead Software Architect and Evaluator for {project_name}.\n"
        f"Operating repository: {canonical_path} (READ-ONLY).\n"
        "Your duty: Evaluate proposed ideas thoroughly, explore architectural alternatives creatively, "
        "and anchor findings to verified repository evidence without stifling innovation."
    )

    user_prompt = f"""
=== INCOMING PROPOSAL ===
- Identifier: {ident}
- Title: {title}
- Description:
{description}

=== PRE-FLIGHT EVIDENCE (From Graphify Knowledge Graph) ===
{graph_text}

=== TEAM ISSUE OVERLAPS (From Linear) ===
{overlap_text}

=== REPOSITORY ARCHITECTURAL INVARIANTS ===
{invariants_text}

=== EVALUATION INSTRUCTIONS ===
Perform a comprehensive architectural brainstorming and context evaluation:
1. EXPLORATION & DESIGN OPTIONS:
   - Propose 2 viable technical approaches (Option A vs Option B) with concrete pros, cons, and tradeoffs.
   - What existing modules/readers in the codebase can be reused?
2. CODEBASE GROUNDING & PRIOR ART:
   - Did Graphify discover any existing deferred plans or specs? How does this proposal relate to them?
   - Is this proposal entirely novel, complementary, or does it overlap with an earlier decision?
3. CLASSIFICATION & BOUNDS:
   - Select exactly ONE Infrastructure Type from: Build, Refactor, Integration, Evaluation, Migration, Maintenance.
   - Explicitly define Non-Goals (what MUST NOT be built in this ticket).
4. HUMAN DECISION GATE:
   - State exactly 1 or 2 numbered, high-leverage decision questions for the human operator to choose the preferred direction.

Format your response in GitHub-flavored Markdown using clean typography (no cartoon emojis).
"""

    return {
        "system": system_prompt,
        "user": user_prompt,
    }


def build_execution_prompt(
    issue: dict,
    project_config: dict,
    plan_content: str,
    dossier: dict,
    human_feedback: str | None = None,
) -> dict:
    """Build standardized master execution prompt injecting Ponytail, Graphify seams, and Anti-Test-Spam rules."""
    ident = issue.get("identifier", "UNKNOWN")
    title = issue.get("title", "")
    canonical_path = project_config.get("canonical_path", "")

    # Graphify key nodes
    graph_nodes = dossier.get("graphify_evidence", {}).get("nodes", [])
    graph_summary = [f"- {n['name']} ({n.get('metadata', '')})" for n in graph_nodes[:10]]
    graph_text = "\n".join(graph_summary) if graph_summary else "Follow existing package hierarchy."

    # Invariants
    invariants = dossier.get("repository_invariants", [])
    invariants_text = "\n".join([f"- {inv}" for inv in invariants]) if invariants else "Standard clean architecture."

    feedback_section = ""
    if human_feedback and human_feedback.strip():
        feedback_section = f"""
=== HUMAN REVIEWER DELTA FEEDBACK (REWORK / FIX DIRECTIVE) ===
The human reviewer inspected the prior execution/PR and requested the following adjustments:
{human_feedback.strip()}

PRIORITY DIRECTIVE:
Address the human reviewer's feedback directly. Do NOT restart from scratch; build upon existing progress in this worktree and apply surgical fixes or missing test coverage as directed.
"""

    system_prompt = (
        "You are an expert autonomous software engineer executing inside an isolated Git worktree sandbox.\n"
        "Your duty: Implement the approved plan strictly, step by step, following Superpowers TDD practices.\n"
        "Write minimal, elegant code that passes all verification gates."
    )

    user_prompt = f"""
=== TASK BRIEF ===
- Ticket: {ident} — {title}
- Target Repository Sandbox: {canonical_path}
{feedback_section}
=== GOVERNING RULES: PONYTAIL (LAZY SENIOR DEV MODE) ===
1. Does this need to be built at all? (YAGNI).
2. Reuse existing helpers, utilities, and reader profiles in this codebase. Do NOT re-write them.
3. Reach for the standard library and Foundation before introducing custom constructs.
4. Bug fix = Root cause, not symptom. One clean guard in the shared function beats multiple per-caller hacks.
5. Deletion over addition. Boring over clever. Fewest files and shortest working diff wins.

=== ANTI-TEST-SPAM & CLEAN REPO HYGIENE ===
1. DO NOT create loose, temporary, or scattered test files (e.g. `Test123.swift`, `scratch.*`, `DummyTest.swift`).
2. Integrate all new unit tests directly into cohesive, functional test suites inside `Tests/<Target>Tests/`.
3. If temporary scripts or fixtures are created during debugging, DELETE them before declaring completion.

=== CODEBASE SEAMS (From Graphify Knowledge Graph) ===
{graph_text}

=== REPOSITORY INVARIANTS ===
{invariants_text}

=== APPROVED IMPLEMENTATION PLAN ===
{plan_content}

=== EXECUTION DIRECTIVE ===
Execute the tasks outlined in the plan above:
1. For each task: follow TDD (assert test fails RED -> write minimal implementation -> assert test passes GREEN).
2. Run the specified verification commands to confirm 0 compilation errors and clean test suite pass.
3. Keep changes strictly bounded within this worktree.
"""
    return {
        "system": system_prompt,
        "user": user_prompt.strip(),
    }


def format_linear_context_comment(
    ident: str,
    title: str,
    exploration_summary: str,
    infra_type: str,
    session_id: str,
    prior_art_note: str,
    questions: list[str],
    worktree_note: str | None = None,
    absorbed_note: str | None = None,
) -> str:
    """Format the complete, rich Context & Exploration Summary for Linear issue comment."""
    q_lines = "\n".join([f"{i+1}. {q}" for i, q in enumerate(questions)])
    wt_section = ""
    if worktree_note:
        wt_section = f"\n#### 4. Active Worktree & Dependency Scan\n{worktree_note.strip()}\n"

    abs_section = ""
    if absorbed_note:
        abs_section = f"\n#### Absorbed & Merged Backlog Ideas\n{absorbed_note.strip()}\n"

    comment = f"""### Context & Exploration Summary

#### 1. Exploration & Design Options
{exploration_summary.strip()}

#### 2. Prior Art & Codebase Grounding
- **Repository Seams**: Evaluated against existing modules in the repository.
- **Historical Context**: {prior_art_note.strip()}

#### 3. Classification & Bounds
- **Infrastructure Type**: `{infra_type}`
- **Session ID**: `{session_id}`
- **Execution Mode**: Read-only context inspection (No worktree created).
{abs_section}{wt_section}
---

#### Decisions Required Before Planning
{q_lines}

> **STOP**: Reached human approval gate `human:context-review`. Reply with your preferred option and transition to `agent:planning` to begin detailed implementation planning.
"""
    return comment.strip()
