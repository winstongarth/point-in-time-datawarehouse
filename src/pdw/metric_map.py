from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class MetricMapping:
    """Priority-ordered XBRL tags for one canonical metric, and the unit they must be in.

    The first tag in priority order that has data wins, and the unit
    matters because the same tag can appear in multiple units (e.g. a
    duration concept reported per-share) that must not be confused with each
    other.
    """

    unit: str
    tags: tuple[str, ...]


def load_metric_map(path: Path) -> dict[str, MetricMapping]:
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict) or not data:
        raise ValueError(f"metric map {path} is empty or not a mapping")

    result: dict[str, MetricMapping] = {}
    for metric_code, body in data.items():
        result[metric_code] = MetricMapping(unit=body["unit"], tags=tuple(body["tags"]))
    return result
