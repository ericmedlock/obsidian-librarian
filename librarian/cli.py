from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from librarian.config import ensure_librarian_dir, load_config
from librarian.ingest.chunker import chunk_note
from librarian.ingest.embedder import Embedder
from librarian.rag.store import VaultStore
from librarian.rules.engine import FileEvent, RuleEngine
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

    console.print("[bold]Obsidian Librarian — Status[/]\n")

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Key", style="dim")
    table.add_column("Value")

    table.add_row("Vault", cfg.vault_path)
    table.add_row("LM Studio", cfg.lm_studio_url)
    table.add_row("Model", cfg.main_model)
    table.add_row("Autonomous mode", str(cfg.autonomous_mode))
    table.add_row("Chunks indexed", str(store.count()))
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
    """Manually ingest a specific file into the vector store."""
    cfg = _get_cfg()
    if not path.exists():
        console.print(f"[red]File not found:[/] {path}")
        raise typer.Exit(1)

    console.print(f"Ingesting [cyan]{path.name}[/]...")

    fm, chunks = chunk_note(path)
    console.print(f"  Frontmatter keys: {list(fm.keys()) or 'none'}")
    console.print(f"  Chunks: {len(chunks)}")

    embedder = Embedder(cfg)
    embedded = embedder.embed_chunks(chunks)

    store = VaultStore(cfg)
    store.upsert(embedded)

    console.print(f"[green]Done.[/] {len(embedded)} chunks upserted.")


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
