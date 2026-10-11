"""Save and load Settings as JSON presets. The output folder is never saved in a preset."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from readystamp.model import Settings, readable_errors


class PresetError(ValueError):
    def __init__(self, problems: list[str]):
        super().__init__("\n".join(problems))
        self.problems = problems


def to_preset(settings: Settings) -> dict:
    data = json.loads(settings.model_dump_json())
    data["output"].pop("output_dir", None)
    return data


def from_preset(data: dict) -> Settings:
    """Validate preset data; unknown keys are rejected with their names."""
    if not isinstance(data, dict):
        raise PresetError(["A preset must be a JSON object."])
    if isinstance(data.get("output"), dict) and "output_dir" in data["output"]:
        raise PresetError(["Unknown setting 'output_dir' in output (presets do not store the output folder)"])
    try:
        return Settings.model_validate(data)
    except ValidationError as exc:
        raise PresetError(readable_errors(exc)) from exc


def save_preset(settings: Settings, path: Path) -> None:
    Path(path).write_text(json.dumps(to_preset(settings), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_preset(path: Path) -> Settings:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PresetError([f"The preset {path} could not be found."]) from None
    except json.JSONDecodeError as exc:
        raise PresetError([f"The preset {Path(path).name} is not valid JSON (line {exc.lineno}): {exc.msg}."]) from None
    return from_preset(data)
