import threading

from ruamel.yaml import YAML

from librarian.rules.loader import RulesRegistry

_yaml = YAML()

_SEED = """rules:
  - id: rule_001
    name: Backup detection
    type: regex
    pattern: _backup_
    action: flag_as_duplicate
    confidence: 0.95
    hit_count: 0
    created: '2026-01-01'
    description: backups
"""


def _registry(tmp_path):
    p = tmp_path / "rules_registry.yaml"
    p.write_text(_SEED)
    return p, RulesRegistry(p)


def test_increment_persists_to_disk(tmp_path):
    p, reg = _registry(tmp_path)
    reg.increment_hit("rule_001")
    # a freshly loaded registry sees the persisted count
    reloaded = RulesRegistry(p)
    assert reloaded.rules[0].hit_count == 1


def test_concurrent_increments_are_serialized(tmp_path):
    p, reg = _registry(tmp_path)
    N = 50

    def bump():
        reg.increment_hit("rule_001")

    threads = [threading.Thread(target=bump) for _ in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert reg.rules[0].hit_count == N
    # file is still valid YAML (atomic write never left it half-written)
    with open(p) as f:
        data = _yaml.load(f)
    assert data["rules"][0]["hit_count"] == N


def test_atomic_write_leaves_no_temp_files(tmp_path):
    p, reg = _registry(tmp_path)
    reg.increment_hit("rule_001")
    leftovers = list(tmp_path.glob("*.tmp"))
    assert leftovers == []
