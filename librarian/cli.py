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
def run(dry_run: bool = typer.Option(False, "--dry-run", help="Preview only, no writes")):
    """Full organization pass: scan → classify → organize → report."""
    cfg = _get_cfg()
    registry = _get_registry(cfg)

    from librarian.agents.crew import build_crew
    from datetime import datetime

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

    crew = build_crew(cfg, inputs)
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
):
    """Full vault scan: ingest every note into the vector store + metadata graph."""
    cfg = _get_cfg()
    notes = iter_vault_notes(cfg.vault_path)
    if limit:
        notes = notes[:limit]

    console.print(f"Scanning [bold]{len(notes)}[/] notes from {cfg.vault_path}\n")
    pipeline = IngestPipeline(cfg)

    total_chunks = 0
    failed: list[tuple[str, str]] = []
    for note in track(notes, description="Ingesting", console=console):
        try:
            total_chunks += pipeline.ingest_file(note)
        except Exception as exc:  # noqa: BLE001 — report and continue the scan
            failed.append((note.name, str(exc)))

    stats = pipeline.graph.stats()
    pipeline.close()

    console.print(
        f"\n[green]Done.[/] {len(notes) - len(failed)} notes, "
        f"{total_chunks} chunks indexed. "
        f"Graph: {stats['total_notes']} notes, {stats['orphans']} orphans, "
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
