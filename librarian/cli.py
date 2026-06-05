from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.progress import track
from rich.table import Table

from librarian.config import ensure_librarian_dir, load_config
from librarian.pipeline import IngestPipeline, iter_vault_notes
from librarian.rag.graph import VaultGraph
from librarian.rag.store import VaultStore
from librarian.rules.loader import RulesRegistry

app = typer.Typer(name="librarian", help="Obsidian Librarian — local AI vault organizer")
console = Console()


def _get_cfg():
    cfg = load_config()
    ensure_librarian_dir(cfg)
    return cfg


def _get_registry(cfg):
    # Prefer ~/.librarian/rules_registry.yaml; fall back to bundled data/
    path = cfg.rules_registry_path
    if not path.exists():
        bundled = Path(__file__).parent.parent / "data" / "rules_registry.yaml"
        if bundled.exists():
            import shutil
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(bundled, path)
    return RulesRegistry(path)


@app.command()
def init(
    vault: Optional[str] = typer.Option(None, "--vault", help="Path to your Obsidian vault"),
):
    """Create ~/.librarian/config.yaml pointing at your vault (first-time setup)."""
    from ruamel.yaml import YAML

    cfg_path = Path("~/.librarian/config.yaml").expanduser()
    cfg = load_config()  # current effective config (defaults, or an existing file)

    if cfg_path.exists() and not typer.confirm(f"Config exists at {cfg_path}. Overwrite?"):
        raise typer.Exit(0)

    vault_path = vault or typer.prompt("Path to your Obsidian vault", default=cfg.vault_path)
    vault_path = str(Path(vault_path).expanduser())
    vp = Path(vault_path)
    if not vp.exists():
        console.print(f"[yellow]Warning:[/] {vault_path} does not exist yet.")
        if not typer.confirm("Use it anyway?"):
            raise typer.Exit(0)
    elif not (vp / ".git").exists():
        console.print(
            "[yellow]Note:[/] that vault is not a git repo. Autonomous `organize --apply` and "
            "`run` will refuse without --force (no snapshot to restore from). Consider running "
            "[cyan]git init[/] inside the vault."
        )

    lm_url = typer.prompt("LM Studio URL", default=cfg.lm_studio_url)
    model = typer.prompt("Main model", default=cfg.main_model)

    data = {"vault_path": vault_path, "lm_studio_url": lm_url, "main_model": model}
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cfg_path, "w") as f:
        YAML().dump(data, f)
    console.print(f"[green]Wrote[/] {cfg_path}")
    console.print(
        "Next: [cyan]librarian scan[/], then [cyan]librarian organize[/] for a dry-run preview."
    )


@app.command()
def run(
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview only, no writes"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the autonomous-mode confirmation"),
    force: bool = typer.Option(False, "--force", help="Allow autonomous run on a non-git vault"),
):
    """
    LLM-assisted organization pass: scan → classify → organize → report.

    This is the reasoning-heavy path — it drives the CrewAI agents against your
    local LM Studio model. For routine cleanup prefer the deterministic, no-LLM
    `librarian organize`, which only auto-applies safe rule-based moves and
    queues anything ambiguous for your approval.
    """
    from pathlib import Path

    from librarian import safety

    cfg = _get_cfg()
    registry = _get_registry(cfg)

    # Autonomous (non-dry) runs mutate the real vault. Require explicit
    # confirmation unless --yes is passed. Files are only moved (never deleted),
    # and every action is logged to run_log.jsonl.
    if not dry_run and not yes:
        console.print(
            "[bold yellow]Autonomous mode[/] will MOVE files and WRITE frontmatter "
            f"in:\n  {cfg.vault_path}\n[dim](move-only, never deletes; logged to "
            f"{cfg.run_log_path})[/]"
        )
        if not typer.confirm("Proceed with real changes?"):
            console.print("Aborted. (Use [cyan]--dry-run[/] to preview.)")
            raise typer.Exit(0)

    # Safety net: snapshot the vault with git before mutating it, so the whole
    # run is recoverable. Refuse on a non-git vault unless --force.
    if not dry_run:
        vault = Path(cfg.vault_path)
        if safety.is_git_repo(vault):
            sha = safety.snapshot(vault, "librarian: pre-run snapshot")
            console.print(f"[dim]Vault snapshot:[/] {sha[:10]} (restore with git if needed)")
        elif not force:
            console.print(
                "[red]Refusing autonomous run:[/] vault is not a git repo, so changes "
                "wouldn't be recoverable.\nInitialize git in the vault, or re-run with "
                "[cyan]--force[/] if you have another backup."
            )
            raise typer.Exit(1)
        else:
            console.print("[yellow]--force:[/] proceeding on a non-git vault (no snapshot).")

    from librarian.agents.crew import build_crew

    inputs = {
        "vault_path": cfg.vault_path,
        "run_log_path": str(cfg.run_log_path),
        "rules_registry_path": str(cfg.rules_registry_path),
        "report_date": date.today().isoformat(),
        "dry_run": dry_run,
        "stale_threshold_days": cfg.stale_threshold_days,
        "rule_confidence_threshold": cfg.rule_confidence_threshold,
    }

    mode = "[yellow]DRY RUN[/]" if dry_run else "[green]AUTONOMOUS[/]"
    console.print(f"[bold]Obsidian Librarian[/] — {mode}")
    console.print(f"Vault: {cfg.vault_path}\n")

    crew = build_crew(cfg, inputs, registry=registry)
    result = crew.kickoff(inputs=inputs)
    console.print(result)


@app.command(name="dry-run")
def dry_run_cmd():
    """Preview all proposed changes without applying them."""
    run(dry_run=True)


@app.command()
def status():
    """Show vault health summary and last run time."""
    cfg = _get_cfg()
    registry = _get_registry(cfg)
    store = VaultStore(cfg)
    graph = VaultGraph(cfg)
    gstats = graph.stats()
    graph.close()

    console.print("[bold]Obsidian Librarian — Status[/]\n")

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Key", style="dim")
    table.add_column("Value")

    table.add_row("Vault", cfg.vault_path)
    table.add_row("LM Studio", cfg.lm_studio_url)
    table.add_row("Model", cfg.main_model)
    table.add_row("Autonomous mode", str(cfg.autonomous_mode))
    table.add_row("Chunks indexed", str(store.count()))
    table.add_row("Notes in graph", str(gstats["total_notes"]))
    table.add_row("Orphan notes", str(gstats["orphans"]))
    table.add_row("Wikilinks", str(gstats["total_links"]))
    table.add_row("Rules loaded", str(len(registry.rules)))

    # Last run from run_log
    last_run = "never"
    if cfg.run_log_path.exists():
        lines = cfg.run_log_path.read_text().strip().splitlines()
        if lines:
            try:
                last_entry = json.loads(lines[-1])
                last_run = last_entry.get("timestamp", "unknown")
            except json.JSONDecodeError:
                pass
    table.add_row("Last run", last_run)

    console.print(table)


@app.command()
def ingest(path: Path = typer.Argument(..., help="Path to a .md file to force-ingest")):
    """Manually ingest a specific file into the vector store + metadata graph."""
    cfg = _get_cfg()
    if not path.exists():
        console.print(f"[red]File not found:[/] {path}")
        raise typer.Exit(1)

    console.print(f"Ingesting [cyan]{path.name}[/]...")
    pipeline = IngestPipeline(cfg)
    n = pipeline.ingest_file(path)
    pipeline.close()
    console.print(f"[green]Done.[/] {n} chunks upserted.")


@app.command()
def scan(
    limit: Optional[int] = typer.Option(None, help="Only ingest the first N notes (for testing)"),
    no_embed: bool = typer.Option(
        False, "--no-embed", help="Graph metadata only — skip embeddings (fast)"
    ),
):
    """Full vault scan: ingest every note into the vector store + metadata graph."""
    cfg = _get_cfg()
    notes = iter_vault_notes(cfg.vault_path)
    if limit:
        notes = notes[:limit]

    how = "metadata only" if no_embed else "embed + metadata"
    console.print(f"Scanning [bold]{len(notes)}[/] notes from {cfg.vault_path} ({how})\n")
    pipeline = IngestPipeline(cfg)

    total_chunks = 0
    failed: list[tuple[str, str]] = []
    for note in track(notes, description="Scanning", console=console):
        try:
            if no_embed:
                pipeline.index_metadata_only(note)
            else:
                total_chunks += pipeline.ingest_file(note)
        except Exception as exc:  # noqa: BLE001 — report and continue the scan
            failed.append((note.name, str(exc)))

    # A full scan should leave the graph reflecting exactly what's on disk:
    # drop rows for notes deleted/moved out of band. (Skip for partial scans.)
    pruned = 0
    if not limit:
        pruned = len(pipeline.graph.prune_missing())

    stats = pipeline.graph.stats()
    pipeline.close()

    console.print(
        f"\n[green]Done.[/] {len(notes) - len(failed)} notes, "
        f"{total_chunks} chunks indexed"
        + (f", {pruned} stale rows pruned" if pruned else "")
        + f". Graph: {stats['total_notes']} notes, {stats['orphans']} orphans, "
        f"{stats['total_links']} links."
    )
    if failed:
        console.print(f"[yellow]{len(failed)} notes failed:[/]")
        for name, err in failed[:10]:
            console.print(f"  [red]{name}[/]: {err}")


@app.command()
def watch():
    """Watch the vault and incrementally ingest changes until interrupted."""
    import time

    from librarian.ingest.watcher import VaultWatcher

    cfg = _get_cfg()
    registry = _get_registry(cfg)
    pipeline = IngestPipeline(cfg, registry=registry)

    def on_upsert(p: Path) -> None:
        try:
            match = pipeline.apply_rules(p)
            n = pipeline.ingest_file(p)
            tag = f" → rule {match.rule.id} ({match.action})" if match else ""
            console.print(f"[green]+[/] {p.name}: {n} chunks{tag}")
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]![/] {p.name}: {exc}")

    def on_delete(p: Path) -> None:
        pipeline.delete_file(p)
        console.print(f"[dim]-[/] {p.name}: removed from index")

    watcher = VaultWatcher(cfg, on_upsert, on_delete)
    watcher.start()
    console.print(f"[bold]Watching[/] {cfg.vault_path} (Ctrl-C to stop)\n")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        console.print("\nStopping...")
    finally:
        watcher.stop()
        pipeline.close()


@app.command()
def audit(
    write: bool = typer.Option(False, "--write", help="Write a Markdown report into the vault"),
    rule_limit: int = typer.Option(0, help="Limit rule scan to first N notes (0 = all)"),
):
    """
    Deterministic, rule-first dry-run audit — no LLM. Runs the rule registry
    across the vault and reports actionable findings from the metadata graph
    (orphans, missing frontmatter, stale notes). Fast and reliable; this is the
    token-free layer the LLM agents escalate from.
    """
    import time

    from librarian.rules.engine import FileEvent, RuleEngine

    cfg = _get_cfg()
    registry = _get_registry(cfg)
    engine = RuleEngine(registry)
    graph = VaultGraph(cfg)

    notes = iter_vault_notes(cfg.vault_path)
    scan = notes[:rule_limit] if rule_limit else notes

    # 1. Rule-engine pass (token-free).
    from collections import Counter

    from librarian.ingest.chunker import parse_note

    action_counts: Counter = Counter()
    rule_hits: list[tuple[str, str, str]] = []
    for note in track(scan, description="Running rules", console=console):
        try:
            fm, body, _ = parse_note(note)
        except Exception:  # noqa: BLE001
            continue
        match = engine.run(FileEvent(path=note, frontmatter=fm, body=body), count_hit=False)
        if match:
            action_counts[match.action] += 1
            rule_hits.append((note.name, match.rule.id, match.action))

    # 2. Graph-derived findings.
    stats = graph.stats()
    orphan_paths = graph.orphans()
    missing_fm = [
        r[0]
        for r in graph._conn.execute(
            "SELECT file_path FROM notes WHERE note_type='' OR status='' LIMIT 100000"
        ).fetchall()
    ]
    cutoff = time.time() - cfg.stale_threshold_days * 86400
    stale_orphans = [
        r[0]
        for r in graph._conn.execute(
            "SELECT n.file_path FROM notes n WHERE n.modified_at IS NOT NULL "
            "AND n.modified_at < ? AND NOT EXISTS "
            "(SELECT 1 FROM links l WHERE l.target_name = _stem(n.file_path))",
            (cutoff,),
        ).fetchall()
    ]
    graph.close()

    # 3. Report.
    console.print("\n[bold]Obsidian Librarian — Audit (dry run, no writes)[/]\n")
    summary = Table(show_header=False, box=None, padding=(0, 2))
    summary.add_column("k", style="dim")
    summary.add_column("v")
    summary.add_row("Notes in graph", str(stats["total_notes"]))
    summary.add_row("Wikilinks", str(stats["total_links"]))
    summary.add_row("Rule hits", f"{len(rule_hits)} (scanned {len(scan)})")
    summary.add_row("Orphans (no in/out links)", str(len(orphan_paths)))
    summary.add_row("Missing frontmatter", str(len(missing_fm)))
    summary.add_row(f"Stale (> {cfg.stale_threshold_days}d, no inbound)", str(len(stale_orphans)))
    console.print(summary)

    if action_counts:
        console.print("\n[bold]Proposed rule actions[/]")
        at = Table(show_header=True, box=None, padding=(0, 2))
        at.add_column("Action", style="cyan")
        at.add_column("Count", justify="right")
        for action, n in action_counts.most_common():
            at.add_row(action, str(n))
        console.print(at)
        for name, rid, action in rule_hits[:15]:
            console.print(f"  [dim]{rid}[/] {action} → {name}")
        if len(rule_hits) > 15:
            console.print(f"  [dim]... and {len(rule_hits) - 15} more[/]")
    else:
        console.print("\n[dim]No rule hits — every scanned note would escalate to the LLM agents.[/]")

    if write:
        report_dir = Path(cfg.vault_path) / "Obsidian Librarian" / "Reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / f"{date.today().isoformat()}-audit.md"
        lines = [
            f"# Vault Audit — {date.today().isoformat()}",
            "",
            f"- Notes: {stats['total_notes']}",
            f"- Wikilinks: {stats['total_links']}",
            f"- Rule hits: {len(rule_hits)}",
            f"- Orphans: {len(orphan_paths)}",
            f"- Missing frontmatter: {len(missing_fm)}",
            f"- Stale (> {cfg.stale_threshold_days}d, no inbound): {len(stale_orphans)}",
            "",
            "## Proposed rule actions",
            *[f"- `{rid}` **{action}** → {name}" for name, rid, action in rule_hits],
        ]
        report_path.write_text("\n".join(lines), encoding="utf-8")
        console.print(f"\n[green]Report written:[/] {report_path}")


def _snapshot_or_refuse(cfg, message: str, force: bool) -> bool:
    """Git-snapshot the vault before mutating it. Returns False (and prints why)
    if the vault isn't a git repo and --force wasn't given."""
    from librarian import safety

    vault = Path(cfg.vault_path)
    if safety.is_git_repo(vault):
        sha = safety.snapshot(vault, message)
        console.print(f"[dim]Vault snapshot:[/] {sha[:10]} (restore with git if needed)")
        return True
    if force:
        console.print("[yellow]--force:[/] proceeding on a non-git vault (no snapshot).")
        return True
    console.print(
        "[red]Refusing to apply:[/] vault is not a git repo, so changes wouldn't be "
        "recoverable.\nInitialize git in the vault, or re-run with [cyan]--force[/] if you "
        "have another backup."
    )
    return False


@app.command()
def organize(
    apply: bool = typer.Option(
        False, "--apply", help="Actually apply changes (default is a safe dry-run preview)"
    ),
    limit: int = typer.Option(0, help="Only process the first N notes (0 = all)"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the apply confirmation"),
    force: bool = typer.Option(False, "--force", help="Allow applying on a non-git vault"),
):
    """
    Deterministic, rule-based organization pass — NO LLM. Auto-applies safe,
    reversible, link-guarded moves and queues anything risky for your approval
    (see `librarian review`). Defaults to a dry-run preview; pass --apply to write.
    """
    from librarian.organizer import organize as run_organize
    from librarian.pending import PendingActions

    cfg = _get_cfg()
    registry = _get_registry(cfg)
    dry_run = not apply

    if apply and not yes:
        console.print(
            "[bold yellow]Apply mode[/] will MOVE files in:\n"
            f"  {cfg.vault_path}\n[dim](safe rule moves only — risky actions are queued, never "
            f"auto-applied; move-only, never deletes; logged to {cfg.run_log_path})[/]"
        )
        if not typer.confirm("Proceed with real changes?"):
            console.print("Aborted. (Omit [cyan]--apply[/] to preview.)")
            raise typer.Exit(0)

    if apply and not _snapshot_or_refuse(cfg, "librarian: pre-organize snapshot", force):
        raise typer.Exit(1)

    pending = PendingActions(cfg)
    try:
        summary = run_organize(cfg, registry, pending, dry_run=dry_run, limit=limit)
    finally:
        pending.close()

    mode = "[yellow]DRY RUN[/]" if dry_run else "[green]APPLIED[/]"
    console.print(f"\n[bold]Organize[/] — {mode}  (scanned {summary['scanned']})")
    t = Table(show_header=False, box=None, padding=(0, 2))
    t.add_column("k", style="dim")
    t.add_column("v")
    t.add_row("Auto-applied" + (" (preview)" if dry_run else ""), str(summary["auto_applied"]))
    t.add_row("Queued for review" + (" (would queue)" if dry_run else ""), str(summary["queued"]))
    t.add_row("Skipped / refused", str(summary["skipped"]))
    console.print(t)

    for d in summary["detail"][:15]:
        if d.get("auto"):
            console.print(f"  [cyan]auto[/] {d['note']}: {d['result']}")
        else:
            console.print(f"  [magenta]queue[/] {d['note']}: {d['action']} → {d['target']}")
    if len(summary["detail"]) > 15:
        console.print(f"  [dim]... and {len(summary['detail']) - 15} more[/]")

    queued = summary["queued"]
    if not dry_run and queued:
        console.print(
            f"\n[dim]{queued} action(s) need your approval — run[/] [cyan]librarian review[/]"
        )


@app.command()
def review():
    """List actions queued for your approval by `librarian organize`."""
    from librarian.pending import PendingActions

    cfg = _get_cfg()
    pending = PendingActions(cfg)
    items = pending.list()
    pending.close()

    if not items:
        console.print("No pending actions. [dim]Run `librarian organize --apply` to generate some.[/]")
        return

    table = Table(title="Pending actions", show_header=True, box=None, padding=(0, 2))
    table.add_column("ID", style="cyan", justify="right")
    table.add_column("Action")
    table.add_column("Note")
    table.add_column("Target", style="dim")
    table.add_column("Reason", style="dim")
    for it in items:
        table.add_row(
            str(it["id"]),
            it["action"],
            Path(it["note_path"]).name,
            it.get("target") or "",
            it.get("reason") or "",
        )
    console.print(table)
    console.print(
        "\n[dim]Apply one with[/] [cyan]librarian approve <id>[/][dim], "
        "or dismiss with[/] [cyan]librarian reject <id>[/]"
    )


@app.command()
def approve(
    action_id: int = typer.Argument(..., help="Pending action id (see `librarian review`)"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
    force: bool = typer.Option(False, "--force", help="Allow applying on a non-git vault"),
):
    """Approve and apply a single queued action."""
    from librarian.organizer import apply_pending
    from librarian.pending import PendingActions

    cfg = _get_cfg()
    pending = PendingActions(cfg)
    row = pending.get(action_id)
    if row is None:
        console.print(f"[red]No pending action #{action_id}.[/] Run `librarian review`.")
        pending.close()
        raise typer.Exit(1)

    console.print(
        f"#{action_id}: [bold]{row['action']}[/] → {Path(row['note_path']).name} "
        f"[dim](target: {row['target']})[/]"
    )
    if not yes and not typer.confirm("Apply this action?"):
        console.print("Aborted.")
        pending.close()
        raise typer.Exit(0)

    if not _snapshot_or_refuse(cfg, f"librarian: pre-approve #{action_id}", force):
        pending.close()
        raise typer.Exit(1)

    pending.approve(action_id)
    result = apply_pending(cfg, pending, action_id)
    pending.close()
    console.print(result)


@app.command()
def reject(action_id: int = typer.Argument(..., help="Pending action id to dismiss")):
    """Reject (dismiss) a queued action without applying it."""
    from librarian.pending import PendingActions

    cfg = _get_cfg()
    pending = PendingActions(cfg)
    row = pending.get(action_id)
    if row is None:
        console.print(f"[red]No pending action #{action_id}.[/]")
        pending.close()
        raise typer.Exit(1)
    pending.reject(action_id)
    pending.close()
    console.print(f"Rejected #{action_id}: {row['action']} → {Path(row['note_path']).name}")


@app.command()
def orphans(limit: int = typer.Option(20, help="Max orphans to list")):
    """List notes with no incoming or outgoing wikilinks (needs a prior scan)."""
    cfg = _get_cfg()
    graph = VaultGraph(cfg)
    orphan_paths = graph.orphans()
    stats = graph.stats()
    graph.close()

    console.print(
        f"[bold]{len(orphan_paths)}[/] orphans of {stats['total_notes']} notes\n"
    )
    for p in orphan_paths[:limit]:
        console.print(f"  {Path(p).name}")
    if len(orphan_paths) > limit:
        console.print(f"  [dim]... and {len(orphan_paths) - limit} more[/]")


@app.command()
def query(text: str = typer.Argument(..., help="Query to run against the vault")):
    """Query the vault RAG from the terminal."""
    cfg = _get_cfg()
    store = VaultStore(cfg)

    results = store.query(text, top_k=5, privacy_tier_max=cfg.default_external_tier)
    if not results:
        console.print("No results found.")
        return

    console.print(f"\n[bold]Top results for:[/] {text}\n")
    for i, hit in enumerate(results, 1):
        meta = hit["metadata"]
        source = Path(meta.get("file_path", "unknown")).name
        heading = meta.get("heading") or "(no heading)"
        dist = hit["distance"]
        console.print(f"[bold]{i}.[/] [[{source}]] — {heading}  [dim](dist={dist:.3f})[/]")
        console.print(f"   {hit['text'][:200].strip()}...\n")


@app.command()
def rules(
    list_rules: bool = typer.Option(True, "--list/--no-list"),
):
    """Display all rules in the registry."""
    cfg = _get_cfg()
    registry = _get_registry(cfg)

    table = Table(title="Rules Registry", show_lines=True)
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Name")
    table.add_column("Type", style="dim")
    table.add_column("Action")
    table.add_column("Hits", justify="right")
    table.add_column("Conf", justify="right")
    table.add_column("Description")

    for r in registry.rules:
        table.add_row(
            r.id,
            r.name,
            r.type,
            r.action,
            str(r.hit_count),
            f"{r.confidence:.2f}",
            r.description,
        )

    console.print(table)
