from __future__ import annotations

from pathlib import Path
from typing import Any


class SourceTools:
    DENIED_PATHS = frozenset(
        {
            "assets/scripts/qa/qafaultinjection.cs",
            "assets/tests/editmode/qafaultinjectiontests.cs",
            "config/qa-scenarios.json",
        }
    )
    DENIED_QUERY_TERMS = frozenset(
        {
            "qafaultinjection",
            "qa-scenarios.json",
            "health_ratio_out_of_range",
            "relative_position_mismatch",
            "upgrade_ack_without_effect",
            "chest_collected_without_state_transition",
            "experience_level_drift",
            "currency_leak_across_restart",
        }
    )

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()
        self.assets_root = (self.project_root / "Assets").resolve()

    def search(self, query: str, limit: int = 24) -> dict[str, Any]:
        query = query.strip()
        if not query:
            return {"ok": False, "error": "source_search query is empty"}
        lowered = query.lower()
        if any(term in lowered for term in self.DENIED_QUERY_TERMS):
            return {"ok": False, "error": "Source path is unavailable in the agent channel"}
        matches: list[dict[str, Any]] = []
        for path in self.assets_root.rglob("*.cs"):
            relative = path.relative_to(self.project_root).as_posix().lower()
            if relative in self.DENIED_PATHS:
                continue
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line_number, line in enumerate(lines, 1):
                if lowered in line.lower():
                    matches.append(
                        {
                            "path": path.relative_to(self.project_root).as_posix(),
                            "line": line_number,
                            "text": line.strip()[:300],
                        }
                    )
                    if len(matches) >= limit:
                        return {"ok": True, "query": query, "matches": matches, "truncated": True}
        return {"ok": True, "query": query, "matches": matches, "truncated": False}

    def read(self, relative_path: str, line_start: int = 1, line_count: int = 120) -> dict[str, Any]:
        candidate = (self.project_root / relative_path).resolve()
        try:
            candidate.relative_to(self.project_root)
        except ValueError:
            return {"ok": False, "error": "Path escapes the project root"}
        normalized = candidate.relative_to(self.project_root).as_posix().lower()
        if normalized in self.DENIED_PATHS:
            return {"ok": False, "error": "Source path is unavailable in the agent channel"}
        if candidate.suffix.lower() != ".cs" or not candidate.is_file():
            return {"ok": False, "error": f"C# source file not found: {relative_path}"}
        lines = candidate.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(1, line_start)
        count = min(max(1, line_count), 200)
        end = min(len(lines), start - 1 + count)
        numbered = [f"{index}: {lines[index - 1]}" for index in range(start, end + 1)]
        return {
            "ok": True,
            "path": candidate.relative_to(self.project_root).as_posix(),
            "line_start": start,
            "line_end": end,
            "content": "\n".join(numbered),
        }
