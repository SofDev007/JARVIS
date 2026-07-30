"""Task connector plugin: a local JSON task list.

The simplest connector that is actually useful — and the template for a
real one (Todoist/Asana/…): swap the JSON file for API calls, keep the
same three declared actions, put the API token in the secrets manager
and read it with ``api.secret("todoist_token")``.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Mapping

from digital_twin.automation.registry import ActionSpec
from digital_twin.security.permissions import RiskLevel


def register(api) -> None:
    tasks_file = Path(str(api.config.get("tasks_file", "data/tasks.json")))

    def _load() -> list[dict[str, Any]]:
        if not tasks_file.exists():
            return []
        try:
            data = json.loads(tasks_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot read task list: {exc}") from exc
        return data if isinstance(data, list) else []

    def _save(tasks: list[dict[str, Any]]) -> None:
        tasks_file.parent.mkdir(parents=True, exist_ok=True)
        tasks_file.write_text(
            json.dumps(tasks, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )

    # -- add_task ---------------------------------------------------------
    def validate_add(params: Mapping[str, Any]) -> None:
        text = params.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("a non-empty 'text' string is required")
        if len(text) > 500:
            raise ValueError("'text' exceeds 500 characters")

    def handle_add(params: Mapping[str, Any]) -> str:
        tasks = _load()
        tasks.append({"text": str(params["text"]).strip(),
                      "done": False, "created_at": time.time()})
        _save(tasks)
        return f"added task {len(tasks)}: {params['text']}"

    api.register_action(ActionSpec(
        name="add_task", description="Add a task to the local task list.",
        risk=RiskLevel.SENSITIVE, handler=handle_add, validate=validate_add,
    ))

    # -- list_tasks ---------------------------------------------------------
    def handle_list(params: Mapping[str, Any]) -> str:
        tasks = _load()
        open_tasks = [(index + 1, task) for index, task in enumerate(tasks)
                      if not task.get("done")]
        if not open_tasks:
            return "no open tasks"
        listing = "; ".join(f"#{number} {task['text']}"
                            for number, task in open_tasks[:15])
        return f"{len(open_tasks)} open task(s): {listing}"

    api.register_action(ActionSpec(
        name="list_tasks", description="List open tasks.",
        risk=RiskLevel.SENSITIVE, handler=handle_list,
    ))

    # -- complete_task ---------------------------------------------------------
    def validate_complete(params: Mapping[str, Any]) -> None:
        number = params.get("number")
        if not isinstance(number, int) or number < 1:
            raise ValueError("'number' must be a positive integer")

    def handle_complete(params: Mapping[str, Any]) -> str:
        tasks = _load()
        number = int(params["number"])
        if number > len(tasks):
            raise ValueError(f"no such task: #{number}")
        task = tasks[number - 1]
        if task.get("done"):
            return f"task #{number} was already done"
        task["done"] = True
        task["completed_at"] = time.time()
        _save(tasks)
        return f"completed task #{number}: {task['text']}"

    api.register_action(ActionSpec(
        name="complete_task", description="Mark one task as done.",
        risk=RiskLevel.SENSITIVE, handler=handle_complete,
        validate=validate_complete,
    ))
