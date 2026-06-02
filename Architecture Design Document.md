# Obsidian Librarian — Architecture Design Document

**Version:** 0.1 (Draft)
**Date:** 2026-06-02
**Status:** In Design

---

## 1. System Philosophy

The Obsidian Librarian is a locally-running, AI-powered system that serves two distinct but unified purposes:

1. **Personal Knowledge Management** — continuously organizes, enriches, and maintains the Obsidian vault without requiring manual discipline from the user
2. **Home AI Infrastructure Backend** — serves as the long-term memory, RAG context store, and knowledge API for all local LLMs and AI assistants on the home network

The design principle is **write freely, let the system impose order**. The user never touches folder structure, tags, or links. The system handles it all, learns from every decision, and gets cheaper to run over time as deterministic rules replace LLM calls.

---

## 2. High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        USER INTERFACES                          │
│   macOS Menubar App (rumps)    │    Web UI (FastAPI + HTML)     │
└────────────────┬───────────────┴──────────────┬────────────────┘
                 │                              │
┌────────────────▼──────────────────────────────▼────────────────┐
│                       CORE API LAYER                            │
│              FastAPI — OpenAI-compatible endpoints              │
│         /v1/chat, /v1/organize, /v1/query, /health             │
└──────┬──────────────────┬──────────────────┬───────────────────┘
       │                  │                  │
┌──────▼──────┐  ┌────────▼───────┐  ┌──────▼──────────────────┐
│  INGEST     │  │  CREWAI AGENT  │  │   RAG / QUERY ENGINE    │
│  PIPELINE   │  │     LAYER      │  │                         │
│             │  │                │  │  ChromaDB (vectors)     │
│  watchdog   │  │  Scanner       │  │  nomic-embed-text       │
│  FSEvents   │  │  Classifier    │  │  Hybrid search          │
│  Chunker    │  │  RuleWriter    │  │  (semantic + BM25)      │
│  Embedder   │  │  Organizer     │  │                         │
└──────┬──────┘  │  Reporter      │  └──────────────────────────┘
       │         └────────┬───────┘
       │                  │
┌──────▼──────────────────▼───────────────────────────────────────┐
│                      LLM LAYER                                  │
│         LM Studio — Qwen2.5 32B Q4_K_M (port 1234)            │
│         LM Studio — nomic-embed-text (port 1234)               │
│         OpenAI-compatible REST API                              │
└─────────────────────────────────────────────────────────────────┘
       │
┌──────▼──────────────────────────────────────────────────────────┐
│                    PERSISTENCE LAYER                            │
│  Obsidian Vault (markdown files, source of truth)              │
│  ChromaDB (vector store, ~/.librarian/chroma/)                 │
│  rules_registry.yaml (learned rules)                           │
│  run_log.jsonl (audit trail)                                   │
│  librarian.db (SQLite: note metadata, graph edges)             │
└─────────────────────────────────────────────────────────────────┘
```

---

## 3. Component Specifications

### 3.1 Ingest Pipeline

**Trigger sources:**
- FSEvents watcher (`watchdog.FSEventsObserver`) — fires on file create/modify/delete in vault
- Scheduled batch run via `launchd` plist — nightly full re-scan
- Manual trigger via menubar or CLI

**Processing steps:**

```
Raw .md file
    → parse frontmatter (YAML)
    → heading-aware chunker (split on H2/H3, ~512 token target)
    → embed each chunk (nomic-embed-text via LM Studio)
    → upsert into ChromaDB with metadata:
        {file_path, heading, modified_at, word_count, chunk_index}
    → update librarian.db (note metadata table)
    → enqueue for agent processing if new/changed
```

**Chunking strategy:**
- Split on H2/H3 headings — one heading section = one chunk
- Fallback: sliding window at 512 tokens with 64-token overlap for headingless notes
- Preserve frontmatter as a separate metadata chunk, not embedded
- Respect Obsidian wikilinks — do not split mid-link

---

### 3.2 CrewAI Agent Layer

Five specialized agents, each with a defined role, tools, and LLM access. All agents call Qwen2.5 32B via LM Studio's OpenAI-compatible endpoint.

#### Agent 1: Scanner
- **Role:** Vault surveyor
- **Inputs:** List of new/modified files from ingest queue
- **Tools:** `read_note`, `list_directory`, `get_metadata`
- **Outputs:** Structured report of anomalies — loose files, naming violations, duplicates, orphans
- **LLM usage:** Low (mostly deterministic rule checks first)

#### Agent 2: Classifier
- **Role:** Tagger, categorizer, frontmatter enricher
- **Inputs:** Note content + existing vault taxonomy
- **Tools:** `read_note`, `query_rag`, `list_tags`, `write_frontmatter`
- **Outputs:** Suggested or applied frontmatter: `tags`, `topic`, `status`, `note_type`, `privacy_tier`
- **LLM usage:** Medium (runs deterministic tag rules first, LLM handles ambiguous cases)

#### Agent 3: RuleWriter
- **Role:** Pattern discoverer and rule codifier — the "learning" agent
- **Inputs:** Recent LLM decisions from run_log.jsonl
- **Tools:** `read_run_log`, `write_rule`, `test_rule`, `read_rules_registry`
- **Outputs:** New entries in `rules_registry.yaml`
- **Trigger:** Runs after every 25 LLM decisions, or when confidence threshold is met
- **LLM usage:** High (this is where the intelligence lives)
- **Key behavior:** Evaluates whether a recent LLM decision is generalizable. If yes, writes a Python callable or regex rule so future identical cases cost zero tokens.

#### Agent 4: Organizer
- **Role:** Executor of structural changes
- **Inputs:** Scanner anomalies + Classifier suggestions + Rule Registry
- **Tools:** `move_note`, `rename_note`, `create_folder`, `update_frontmatter`, `create_wikilink`, `archive_note`
- **Outputs:** Applied changes to vault; entries in run_log.jsonl
- **LLM usage:** Low in steady state (rules cover most cases); falls back to LLM for novel situations
- **Safety:** All destructive actions gated by dry-run flag and confirmation in non-autonomous mode

#### Agent 5: Reporter
- **Role:** Digest writer and health monitor
- **Inputs:** run_log.jsonl since last report
- **Tools:** `read_run_log`, `write_note`, `query_rag`
- **Outputs:** Vault health note written to `Obsidian Librarian/Reports/YYYY-MM-DD.md`
- **LLM usage:** Low (templated summaries)

---

### 3.3 Rule Registry System

This is the core of the "learns over time / reduces token usage" design.

**File:** `~/.librarian/rules_registry.yaml`

**Rule schema:**
```yaml
rules:
  - id: rule_001
    name: "backup_file_detection"
    created: 2026-06-02
    created_by: RuleWriter
    hit_count: 47
    confidence: 0.98
    type: regex          # regex | python_callable | frontmatter_pattern
    pattern: "_backup_\\d{8}_\\d{6}\\.md$"
    action: flag_as_duplicate
    description: "Files ending in _backup_YYYYMMDD_HHMMSS.md are redundant backups"

  - id: rule_002
    name: "daily_note_detection"
    created: 2026-06-03
    hit_count: 120
    confidence: 0.99
    type: regex
    pattern: "^Daily - \\d{2}-\\d{2}-\\d{4}\\.md$"
    action: move_to_folder
    target_folder: "Daily Notes"
    description: "Loose daily notes at root should be consolidated"
```

**Execution flow:**
```
New file event
    → run ALL rules in registry (zero LLM cost)
    → if rule matches → execute action → log → done
    → if no rule matches → send to appropriate CrewAI agent
    → agent decision → RuleWriter evaluates for generalizability
    → if generalizable → write new rule
    → next time: rule handles it (zero tokens)
```

**Rule types supported:**
- `regex` — filename/path pattern matching
- `frontmatter_pattern` — match on YAML frontmatter fields
- `content_pattern` — regex on note body
- `python_callable` — arbitrary Python function for complex logic (e.g., "notes with >5 external links should be tagged `reference`")

---

### 3.4 RAG / Query Engine

**Vector store:** ChromaDB (local, persistent, no Docker required)
**Embedding model:** `nomic-embed-text` via LM Studio (768 dimensions)
**Search strategy:** Hybrid — semantic (ChromaDB cosine similarity) + keyword (BM25 via `rank_bm25`)

**Collections in ChromaDB:**
- `vault_notes` — all vault chunks with metadata
- `run_decisions` — past LLM decisions (enables learning and deduplication of reasoning)

**Retrieval API (internal):**
```
query(text, top_k=10, filters={privacy_tier: "public"})
    → embed query with nomic-embed-text
    → semantic search ChromaDB (top 20)
    → BM25 re-rank
    → return top_k chunks with source metadata
```

**External query API** (for home AI infra, on local network):
```
POST http://librarian.local:8080/v1/chat/completions
{
  "model": "obsidian-librarian",
  "messages": [{"role": "user", "content": "What do I know about Avery's ASD therapy?"}]
}
```
Returns answer grounded in vault content with source citations.

---

### 3.5 Privacy Tier System

Notes in the vault have varying sensitivity levels. The Classifier agent assigns a `privacy_tier` to each note:

| Tier | Label | Examples | Exposed via API? |
|------|-------|----------|-----------------|
| 0 | `public` | Tech notes, course notes, how-tos | Yes |
| 1 | `personal` | Goals, projects, ideas | Yes (local network only) |
| 2 | `sensitive` | Therapy notes, medical, financial | No — excluded from RAG API |
| 3 | `private` | Explicitly flagged by user | No — never indexed |

The RAG query API respects tier filters. Home AI assistants default to tiers 0+1 unless explicitly granted higher access.

---

### 3.6 API Layer

Built with **FastAPI**. Serves on `0.0.0.0:8080` (accessible from home network).

**Endpoints:**

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | System status, last run time, vault stats |
| POST | `/v1/chat/completions` | OpenAI-compatible RAG query |
| POST | `/v1/organize` | Trigger manual organization run |
| GET | `/v1/notes` | List notes with metadata/filters |
| GET | `/v1/notes/{id}` | Get note content + embeddings metadata |
| GET | `/v1/rules` | List all rules in registry |
| POST | `/v1/rules` | Add a manual rule |
| GET | `/v1/reports/latest` | Latest vault health report |
| GET | `/v1/graph` | Knowledge graph edges (JSON) |

---

### 3.7 GUI Layer

**macOS Menubar App** (`rumps` library):

```
[📚] ← menubar icon
  ├── Vault Health: ✅ Good (2,357 notes)
  ├── Last Run: Today 3:14 AM
  ├── Pending Actions: 3 items
  ├── ─────────────────
  ├── Run Now
  ├── Dry Run (Preview)
  ├── Pause Auto-Organize
  ├── ─────────────────
  ├── Open Web Dashboard →
  └── Quit
```

**Web Dashboard** (FastAPI + vanilla HTML/JS, served from same process):

- Vault health overview (note count, orphans, untagged, stale)
- Pending actions queue with approve/reject per item
- Rule registry browser (view, edit, disable rules)
- RAG query interface ("ask your vault")
- Run history / audit log
- Knowledge graph visualization (D3.js force graph)

---

### 3.8 Scheduling Architecture

**Event-driven (real-time):**
- `watchdog.FSEventsObserver` watches vault root recursively
- On file create/modify: debounce 5 seconds → enqueue for ingest
- On file delete: remove from ChromaDB + librarian.db

**Scheduled (batch):**
- `launchd` plist runs nightly at 2 AM: full vault re-scan, re-embed stale notes, generate health report
- Weekly: RuleWriter deep review of run log, rule consolidation/pruning

**launchd plist location:**
`~/Library/LaunchAgents/com.librarian.obsidian.plist`

---

### 3.9 Technology Stack

| Layer | Technology | Rationale |
|-------|-----------|-----------|
| Language | Python 3.12 | Full ecosystem coverage for every layer |
| Agent Framework | CrewAI | Multi-agent orchestration, built-in memory, task delegation |
| LLM | Qwen2.5 32B Q4_K_M via LM Studio | Best quality/size for 48GB unified RAM, OpenAI-compatible API |
| Embeddings | nomic-embed-text via LM Studio | Fast, high quality, local, 768-dim |
| Vector Store | ChromaDB | Pure Python, no Docker, persistent, good Python API |
| File Watching | watchdog (FSEventsObserver) | Native macOS FSEvents via Python |
| API | FastAPI | Async, OpenAI-compatible endpoint structure, automatic docs |
| Database | SQLite via SQLAlchemy | Note metadata, graph edges, lightweight |
| Menubar | rumps | Pure Python macOS menubar app |
| Web UI | FastAPI + HTMX + D3.js | Lightweight, no build step, served from same process |
| CLI | Typer | Clean CLI with rich output for manual operations |
| Scheduler | launchd (macOS native) | More reliable than cron on macOS, handles sleep/wake |
| Rule Storage | YAML (ruamel.yaml) | Human-readable, editable, version-controllable |
| Audit Log | JSONL (append-only) | Simple, grep-able, never corrupts |

---

### 3.10 Data Flow: New Note Arrives

```
1. User saves new note in Obsidian
2. FSEvents fires → watchdog catches it
3. Debounce 5s (user may still be typing)
4. Ingest pipeline:
   a. Parse frontmatter + body
   b. Chunk by headings
   c. Embed chunks → upsert ChromaDB
   d. Update librarian.db metadata
5. Rule Registry scan (zero LLM cost):
   a. Run all rules against filename, path, frontmatter
   b. If match → execute action, log, done
6. If no rule match → enqueue for CrewAI
7. Scanner agent reviews note
8. Classifier agent enriches frontmatter
9. Organizer agent applies structural changes (if any)
10. All decisions logged to run_log.jsonl
11. RuleWriter evaluates log batch (every 25 decisions)
    → writes new rules if pattern found
12. Reporter updates health stats
```

---

### 3.11 Data Flow: Home AI Queries Vault

```
1. Home assistant (e.g., Home Assistant + local LLM) sends:
   POST http://librarian.local:8080/v1/chat/completions
   {"messages": [{"role": "user", "content": "What are my notes on parallel computing?"}]}

2. API layer receives request
3. Query embedded with nomic-embed-text
4. Hybrid search: ChromaDB semantic + BM25 keyword
5. Privacy filter applied (default: tiers 0+1 only)
6. Top 8 chunks retrieved with source metadata
7. Qwen2.5 32B synthesizes answer with citations
8. Response returned in OpenAI-compatible format:
   {"choices": [{"message": {"content": "Based on your notes:\n\n[answer]...\n\nSources: [[Parallel Computing ITCS 6145]]"}}]}
```

---

## 4. Directory Structure

```
obsidian-librarian/          ← Python project root
├── librarian/
│   ├── __init__.py
│   ├── config.py            ← vault path, LM Studio URL, ports, etc.
│   ├── ingest/
│   │   ├── watcher.py       ← FSEvents watchdog
│   │   ├── chunker.py       ← heading-aware chunker
│   │   └── embedder.py      ← nomic-embed-text client
│   ├── agents/
│   │   ├── scanner.py
│   │   ├── classifier.py
│   │   ├── rule_writer.py
│   │   ├── organizer.py
│   │   └── reporter.py
│   ├── rules/
│   │   ├── engine.py        ← rule execution at runtime
│   │   ├── loader.py        ← reads rules_registry.yaml
│   │   └── writer.py        ← RuleWriter agent writes here
│   ├── rag/
│   │   ├── store.py         ← ChromaDB interface
│   │   ├── search.py        ← hybrid search
│   │   └── graph.py         ← knowledge graph (SQLite)
│   ├── api/
│   │   ├── main.py          ← FastAPI app
│   │   ├── routes/
│   │   └── static/          ← Web UI assets
│   ├── gui/
│   │   └── menubar.py       ← rumps menubar app
│   └── cli.py               ← Typer CLI entry point
├── data/                    ← ~/.librarian/ symlink
│   ├── chroma/              ← ChromaDB persistent storage
│   ├── rules_registry.yaml
│   ├── run_log.jsonl
│   └── librarian.db
├── com.librarian.obsidian.plist  ← launchd config
├── pyproject.toml
└── README.md
```

---

## 5. Key Design Decisions & Rationale

**Why ChromaDB over Qdrant?**
Qdrant requires Docker or a separate process. ChromaDB is pure Python, embedded, zero infrastructure. For a single-user local system on a Mac, ChromaDB is simpler with no meaningful quality tradeoff.

**Why no LLM router?**
With 48GB unified RAM, Qwen2.5 32B runs fast enough for all tasks including classification. A router adds complexity; the rule registry already handles the "fast path" for cheap cases.

**Why YAML for rules, not a database?**
Rules need to be human-readable and editable. A YAML file can be opened in Obsidian or a text editor, version-controlled with git, and reviewed without tooling. The hit counts and confidence scores written back by the system make it a living document.

**Why HTMX over React for the web UI?**
No build step. FastAPI serves both the API and the UI from one process. The dashboard doesn't need SPA complexity — it's a local tool, not a product.

**Why launchd over cron?**
`launchd` is macOS-native, handles wake-from-sleep correctly (cron misses runs if the machine was asleep), and integrates with the OS security model.

---

## 6. Future Expansion Points

- **Multi-vault support** — config can point to multiple vault roots
- **Ingestion from external sources** — email, web clips, PDFs via a watched inbox folder
- **Git-backed vault** — auto-commit on each librarian run for full history
- **Home Assistant integration** — expose `/v1/chat` as a custom conversation agent in HA
- **Mobile** — the FastAPI layer is accessible from iPhone on home WiFi already
- **Fine-tuning** — run_log.jsonl accumulates (input, decision) pairs that could eventually fine-tune a smaller local model specialized for this vault
