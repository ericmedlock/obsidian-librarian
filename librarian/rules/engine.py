from __future__ import annotations

import importlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from librarian.rules.loader import Rule, RulesRegistry


@dataclass
class FileEvent:
    path: Path
    frontmatter: dict[str, Any]
    body: str


@dataclass
class RuleMatch:
    rule: Rule
    action: str
    target_folder: Optional[str]


class RuleEngine:
    def __init__(self, registry: RulesRegistry) -> None:
        self._registry = registry

    def run(self, event: FileEvent, count_hit: bool = True) -> Optional[RuleMatch]:
        """
        Run all rules against a file event in registration order.
        Returns the first match, or None if no rule covers this case.

        `count_hit` persists an incremented hit_count for the matched rule.
        Read-only passes (audit, dry-run previews, the LLM's scan tool) pass
        count_hit=False so merely *looking* at the vault doesn't mutate the
        registry — hit_count should reflect rules that actually fired on a
        real change, not scans.
        """
        filename = event.path.name
        for rule in self._registry.rules:
            if self._matches(rule, filename, event.frontmatter, event.body):
                if count_hit:
                    self._registry.increment_hit(rule.id)
                return RuleMatch(
                    rule=rule,
                    action=rule.action,
                    target_folder=rule.target_folder,
                )
        return None

    def _matches(
        self,
        rule: Rule,
        filename: str,
        fm: dict[str, Any],
        body: str,
    ) -> bool:
        match rule.type:
            case "regex":
                return bool(re.search(rule.pattern, filename))
            case "frontmatter_pattern":
                return self._match_frontmatter(rule.pattern, fm)
            case "content_pattern":
                return bool(re.search(rule.pattern, body, re.MULTILINE))
            case "python_callable":
                return self._call_python(rule.pattern, filename, fm, body)
            case _:
                return False

    @staticmethod
    def _match_frontmatter(pattern: str, fm: dict[str, Any]) -> bool:
        """
        Pattern format: "field=value" or "field~=regex_value".
        Examples: "note_type=daily", "tags~=ITCS"
        """
        if "~=" in pattern:
            field, regex = pattern.split("~=", 1)
            val = str(fm.get(field.strip(), ""))
            return bool(re.search(regex.strip(), val))
        if "=" in pattern:
            field, value = pattern.split("=", 1)
            return str(fm.get(field.strip(), "")) == value.strip()
        return False

    @staticmethod
    def _call_python(callable_src: str, filename: str, fm: dict, body: str) -> bool:
        """
        callable_src is a dotted module path: "librarian.rules.custom.my_rule".
        The function must accept (filename, frontmatter, body) -> bool.
        """
        try:
            module_path, fn_name = callable_src.rsplit(".", 1)
            mod = importlib.import_module(module_path)
            fn = getattr(mod, fn_name)
            return bool(fn(filename, fm, body))
        except Exception:
            return False
