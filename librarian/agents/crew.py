from __future__ import annotations

from crewai import Agent, Crew, LLM, Task

from librarian.agents.actions import build_write_tools
from librarian.agents.tools import build_vault_tools
from librarian.config import Config
from librarian.rules.loader import RulesRegistry


def _llm(cfg: Config) -> LLM:
    return LLM(
        model=f"openai/{cfg.main_model}",
        base_url=cfg.lm_studio_url,
        api_key="not-needed",
        temperature=0.1,
        # qwen3.6-27b is a reasoning model. Left on, it spends its whole token
        # budget on hidden reasoning and returns empty `content`
        # (finish_reason=length) → CrewAI's "Invalid response from LLM call -
        # None or empty", and otherwise parrots the ReAct template placeholder.
        # `reasoning_effort="none"` disables thinking on this LM Studio build
        # (verified: reasoning_content length 0, direct well-formed answers).
        reasoning_effort="none",
        max_tokens=4000,
    )


def build_crew(cfg: Config, inputs: dict, registry: RulesRegistry | None = None) -> Crew:
    llm = _llm(cfg)

    if registry is None:
        registry = RulesRegistry(cfg.rules_registry_path)
    tools = build_vault_tools(cfg, registry)

    # Write tools are gated on dry_run; default to the SAFE dry-run if unset.
    dry_run = bool(inputs.get("dry_run", True))
    write_tools = build_write_tools(cfg, dry_run)
    read_note_tool = [t for t in tools if t.name == "read_note"]

    scanner = Agent(
        role="Obsidian Vault Inspector",
        goal=(
            "Perform a thorough structural audit of the Obsidian vault at {vault_path}. "
            "Run all rules from the rules registry first. Only escalate to reasoning when "
            "no rule covers a case. Identify: misplaced files, naming violations, orphaned "
            "notes (no incoming or outgoing links), duplicate or backup files, and notes "
            "missing frontmatter. Produce a structured anomaly report — do not fix anything."
        ),
        backstory=(
            "You are a meticulous archivist who has spent years building and maintaining "
            "knowledge systems. You understand Obsidian's file structure, wikilink conventions, "
            "and frontmatter standards deeply. You are fast and systematic — you run deterministic "
            "checks before reaching for reasoning, and you never modify files, only observe and report."
        ),
        llm=llm,
        tools=tools,
        verbose=True,
    )

    classifier = Agent(
        role="Note Enrichment Specialist",
        goal=(
            "For each note flagged by the Scanner as missing or incomplete frontmatter, "
            "read the note content and assign: tags, topic, note_type, status, and privacy_tier. "
            "Sensitive content categories that trigger tier 2: therapy, medical, diagnosis, "
            "medication, financial, legal, credentials. Never assign tier 3."
        ),
        backstory=(
            "You are a librarian and knowledge graph specialist who understands how to read a note "
            "and instantly understand what it is, what it's about, and how sensitive it is. "
            "You are consistent — you look at what tags and topics already exist in the vault "
            "before inventing new ones."
        ),
        llm=llm,
        tools=[t for t in tools if t.name in ("read_note", "list_notes_missing_frontmatter")],
        verbose=True,
    )

    rule_writer = Agent(
        role="Pattern Distillation Engineer",
        goal=(
            "Analyze recent LLM decisions logged in {run_log_path} and identify patterns that are "
            "deterministic enough to be codified as rules. Write qualifying patterns as new entries "
            "in {rules_registry_path}. Do not write rules with confidence below {rule_confidence_threshold}."
        ),
        backstory=(
            "You are a compiler engineer who sees patterns in chaos. Your job is to turn expensive "
            "LLM reasoning into free deterministic code. Every rule you write permanently reduces "
            "the token cost of the system. You are conservative — you only codify patterns you are "
            "certain about."
        ),
        llm=llm,
        verbose=False,
    )

    organizer = Agent(
        role="Vault Restructuring Agent",
        goal=(
            "Execute the structural changes prescribed by the Scanner and Classifier. "
            "In dry_run mode ({dry_run}), describe every action you would take but do not write any files. "
            "In autonomous mode, execute all changes and log each one to {run_log_path}. "
            "Never delete files — only move to Archive."
        ),
        backstory=(
            "You are a careful but decisive executor. You do exactly what is prescribed, nothing more. "
            "You log before you act. You never delete — you archive. You treat the vault as the user's "
            "irreplaceable second brain."
        ),
        llm=llm,
        tools=write_tools[:3] + read_note_tool,  # move_note, archive_note, write_frontmatter, read_note
        verbose=True,
    )

    reporter = Agent(
        role="Vault Health Analyst",
        goal=(
            "After the organization pass is complete, produce a vault health report written as a "
            "Markdown note saved to {vault_path}/Obsidian Librarian/Reports/{report_date}.md. "
            "The report must be readable in under 60 seconds."
        ),
        backstory=(
            "You are a clear communicator who turns system logs into human-readable insight. "
            "You understand that the user has AuDHD and values brevity and clarity over completeness. "
            "Your reports are skimmable, honest, and actionable."
        ),
        llm=llm,
        tools=[t for t in write_tools if t.name == "write_report"],
        verbose=True,
    )

    scan_task = Task(
        description=(
            "Audit the Obsidian vault at {vault_path}.\n\n"
            "Step 1 — Run the rules registry against every file. Record each rule match: "
            "file path, rule id, prescribed action.\n\n"
            "Step 2 — For files with no rule match, reason about each file: "
            "correct folder? naming conventions? frontmatter present and complete? "
            "incoming or outgoing wikilinks? duplicate or backup? "
            "untouched for more than {stale_threshold_days} days?\n\n"
            "Step 3 — Produce a structured anomaly list with: file_path, anomaly_type "
            "(misplaced | missing_frontmatter | orphan | duplicate | stale | naming_violation), "
            "severity (high | medium | low), and recommended_action.\n\n"
            "Do not modify any files."
        ),
        expected_output=(
            "A structured JSON-compatible list of anomalies with fields: file_path, "
            "anomaly_type, severity, recommended_action, and rule_id (null if LLM-reasoned). "
            "Plus a summary: total files scanned, total rule hits, total LLM-reasoned decisions, "
            "anomaly count by type."
        ),
        agent=scanner,
    )

    classify_task = Task(
        description=(
            "For each note in the anomaly list where anomaly_type includes 'missing_frontmatter', "
            "read the note content at {vault_path} and determine: "
            "tags (1-5, from existing vault taxonomy), topic (single primary topic), "
            "note_type (reference|fleeting|literature|permanent|daily|project|resource), "
            "status (draft|active|archived), "
            "privacy_tier (0-2, tier 2 if therapy/medical/financial/legal/credentials detected).\n\n"
            "Output the proposed frontmatter for each note as a YAML block. Do not write to files."
        ),
        expected_output=(
            "A list of frontmatter proposals, one per note: file_path and a complete YAML "
            "frontmatter block ready to be prepended to the note. Flag any notes where "
            "privacy_tier was elevated to 2 with a brief explanation."
        ),
        agent=classifier,
        context=[scan_task],
    )

    organize_task = Task(
        description=(
            "Execute the structural changes prescribed by the scan and classification tasks.\n\n"
            "MOVES: Move misplaced files to their correct folder. Create folder if needed.\n"
            "RENAMES: Rename files violating naming conventions. Never rename daily notes.\n"
            "FRONTMATTER: Write proposed frontmatter. If existing frontmatter, merge — "
            "do not overwrite user-authored fields.\n"
            "ARCHIVES: Move orphaned stale notes to {vault_path}/Archive/ only if no incoming "
            "links AND not modified in more than {stale_threshold_days} days.\n"
            "DUPLICATES: Move backup files to {vault_path}/Archive/Backups/. Do not delete.\n\n"
            "Log every action to {run_log_path} as JSONL.\n"
            "dry_run={dry_run} — if true, produce the log as preview only, write nothing to disk."
        ),
        expected_output=(
            "Summary of all actions taken (or previewed in dry-run): count by action type, "
            "list of moves, renames, frontmatter writes, and archives. "
            "Any action that could not be completed listed with reason."
        ),
        agent=organizer,
        context=[scan_task, classify_task],
    )

    rule_gen_task = Task(
        description=(
            "Review LLM-reasoned decisions from this run (rule_id = null in scan/organize output).\n\n"
            "For each, evaluate: can it be expressed as a regex on filename? "
            "a frontmatter field match? a simple Python condition? "
            "Has a similar decision appeared in previous runs in {run_log_path}?\n\n"
            "Write a new rule to {rules_registry_path} for patterns where:\n"
            "- It has occurred at least 3 times, OR\n"
            "- Confidence >= 0.95 even on first occurrence\n"
            "- Confidence must be >= {rule_confidence_threshold}\n\n"
            "Do not modify existing rules. Only append new ones with next sequential ID."
        ),
        expected_output=(
            "List of new rules written (or 'No new rules generated this run'). "
            "For each rule: id, name, pattern, action, confidence, and rationale."
        ),
        agent=rule_writer,
        context=[scan_task, organize_task],
    )

    report_task = Task(
        description=(
            "Write a vault health report to:\n"
            "{vault_path}/Obsidian Librarian/Reports/{report_date}.md\n\n"
            "Structure:\n"
            "# Vault Health Report — {report_date}\n"
            "## Summary (one paragraph)\n"
            "## Actions Taken (table: Action Type | Count | Notes)\n"
            "## New Rules Generated\n"
            "## Still Needs Attention (max 10 items requiring user decision)\n"
            "## Vault Metrics\n"
            "- Total notes, orphans remaining, untagged remaining, stale notes, "
            "rules in registry, token-free rule hits this run X/X (X%)\n\n"
            "The token-free rule hit % is the most important long-term health indicator."
        ),
        expected_output=(
            "Confirmation that the report was written to the correct path, "
            "plus the full report content as a string."
        ),
        agent=reporter,
        context=[scan_task, organize_task, rule_gen_task],
    )

    return Crew(
        agents=[scanner, classifier, rule_writer, organizer, reporter],
        tasks=[scan_task, classify_task, organize_task, rule_gen_task, report_task],
        verbose=True,
    )
