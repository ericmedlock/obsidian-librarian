# CrewAI Kickoff Prompt — Obsidian Librarian

> Paste this into Claude Code (or any coding LLM) to bootstrap the project.

---

You are building **Obsidian Librarian** — a locally-running, AI-powered system that autonomously organizes an Obsidian vault and serves as a RAG backend for home AI infrastructure. All processing is 100% local. No cloud APIs.

## Hardware & Runtime
- Apple M5 Max, 48GB unified RAM, macOS
- LM Studio running locally at `http://localhost:1234/v1` (OpenAI-compatible)
- Main LLM: `qwen2.5-32b-instruct-q4_k_m`
- Embedding model: `nomic-embed-text-v1.5`
- Python 3.12

## What You Are Building First
Bootstrap the project structure and implement the **Ingest Pipeline** and **Rule Engine** — the two components everything else depends on.

### Step 1: Project scaffold
Create the following layout:
```
obsidian-librarian/
├── librarian/
│   ├── __init__.py
│   ├── config.py
│   ├── ingest/
│   │   ├── watcher.py
│   │   ├── chunker.py
│   │   └── embedder.py
│   ├── agents/
│   │   ├── scanner.py
│   │   ├── classifier.py
│   │   ├── rule_writer.py
│   │   ├── organizer.py
│   │   └── reporter.py
│   ├── rules/
│   │   ├── engine.py
│   │   └── loader.py
│   ├── rag/
│   │   └── store.py
│   └── cli.py
├── data/
│   └── rules_registry.yaml
├── pyproject.toml
└── README.md
```

### Step 2: config.py
Load from `~/.librarian/config.yaml`. Provide defaults. Key fields:
```python
vault_path: str
lm_studio_url: str = "http://localhost:1234/v1"
main_model: str = "qwen2.5-32b-instruct-q4_k_m"
embed_model: str = "nomic-embed-text-v1.5"
autonomous_mode: bool = True
debounce_seconds: int = 5
rule_confidence_threshold: float = 0.85
rule_generation_batch_size: int = 25
stale_threshold_days: int = 90
```

### Step 3: Ingest pipeline
`ingest/chunker.py` — heading-aware chunker:
- Split `.md` files at H2/H3 boundaries
- Each chunk: `{text, heading, file_path, chunk_index, char_start, char_end}`
- Fallback: 512-token sliding window with 64-token overlap for headingless notes
- Parse and preserve YAML frontmatter separately (do not chunk it)

`ingest/embedder.py` — embed chunks:
- Call `nomic-embed-text-v1.5` via LM Studio `/v1/embeddings`
- Return list of `{chunk, embedding, metadata}`

`ingest/watcher.py` — FSEvents file watcher:
- Use `watchdog.observers.FSEventsObserver`
- Watch vault root recursively
- Debounce events by 5 seconds
- On CREATE/MODIFY: enqueue for ingest
- On DELETE: remove from ChromaDB and librarian.db

### Step 4: Rule engine
`rules/loader.py` — load `rules_registry.yaml` into memory at startup, reload on file change.

`rules/engine.py` — execute rules against a file event:
```python
def run_rules(event: FileEvent) -> RuleMatch | None
```
Support four rule types:
- `regex` — match against filename/path
- `frontmatter_pattern` — match against parsed YAML fields
- `content_pattern` — regex against note body
- `python_callable` — load and call a Python function string

Return the first matching rule and its prescribed action. If no match, return None (caller sends to CrewAI agents).

`data/rules_registry.yaml` — seed with these two starter rules:
```yaml
rules:
  - id: rule_001
    name: backup_file_detection
    type: regex
    pattern: "_backup_\\d{8}_\\d{6}\\.md$"
    action: flag_as_duplicate
    confidence: 0.99
    hit_count: 0
    created: 2026-06-02
    description: "Files ending in _backup_YYYYMMDD_HHMMSS.md are redundant backups"

  - id: rule_002
    name: daily_note_at_root
    type: regex
    pattern: "^Daily - \\d{2}-\\d{2}-\\d{4}\\.md$"
    action: move_to_folder
    target_folder: "Daily Notes"
    confidence: 0.99
    hit_count: 0
    created: 2026-06-02
    description: "Loose daily notes at vault root should be consolidated"
```

### Step 5: RAG store
`rag/store.py` — ChromaDB wrapper:
- Collection: `vault_notes`
- `upsert(chunks)` — add/update chunks with metadata
- `delete(file_path)` — remove all chunks for a file
- `query(text, top_k, privacy_tier_max)` — embed query, return top_k chunks filtered by privacy tier

### Step 6: CrewAI crew skeleton
`agents/` — create five CrewAI agents wired to LM Studio. Use `crewai` with `llm` pointed at LM Studio OpenAI-compatible endpoint. Do not implement full agent logic yet — just the crew definition, agent roles, and task stubs so the wiring is testable.

Agents:
1. **Scanner** — surveys vault, runs rule engine, flags anomalies
2. **Classifier** — enriches frontmatter (tags, topic, note_type, privacy_tier)
3. **RuleWriter** — analyzes recent LLM decisions, writes new rules to registry
4. **Organizer** — executes structural changes (move, rename, frontmatter write)
5. **Reporter** — writes vault health digest to `Obsidian Librarian/Reports/YYYY-MM-DD.md`

### Step 7: CLI entry point
`cli.py` using Typer:
- `librarian run` — full pass: scan → classify → organize → report
- `librarian dry-run` — preview only, no writes
- `librarian status` — vault stats + last run time
- `librarian ingest <path>` — force-ingest one file
- `librarian rules list` — print rules registry

## Key constraints
- Vault files are the source of truth. Never modify a file without logging the action first to `~/.librarian/run_log.jsonl`
- Rule engine always runs before the LLM. LLM is called only on rule misses.
- Privacy tiers 2 and 3 are never exposed via any API or returned in RAG results
- All file operations must be idempotent — running twice should not change anything on the second run
- Use `ruamel.yaml` for YAML (preserves comments and formatting)
- Use `chromadb` embedded mode (no server required)
- Use `openai` Python client pointed at LM Studio for both chat and embeddings

## Do not implement yet
- FastAPI layer
- Web dashboard
- Menubar app
- launchd plist
- Full RuleWriter agent logic (just the stub)

## Start here
1. `pyproject.toml` with all dependencies
2. `config.py`
3. `ingest/chunker.py` with tests against a sample markdown file
4. `rules/engine.py` + `rules/loader.py` with the two seed rules
5. Wire it together in `cli.py` so `librarian dry-run` can be run against the vault and print what it would do
