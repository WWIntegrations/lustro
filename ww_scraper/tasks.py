"""Tasks: a saved input plus run options for one actor, run by name.

    storage/tasks/<name>.json   {"name", "actId", "input", "options", "createdAt", "modifiedAt"}

`ww-scraper task run jobs-python-remote` instead of pasting the same JSON every week; a
schedule can point at a task (`ww-scraper schedule --task NAME`). The input is validated
against the actor's schema when the task is saved and again when it runs.
"""
from __future__ import annotations

import json

from .actor import InputError, _now, fill_secrets, get_actor, run_actor, secret_fields, split_options, validate_input
from .storage import Storage, StorageError, atomic_write, check_name


class TaskError(ValueError):
    pass


def _path(storage: Storage, name: str):
    try:
        check_name(name)
    except StorageError as e:
        raise TaskError(str(e).replace("storage name", "task name")) from None
    return storage.root / "tasks" / f"{name}.json"


def _check(actor, data, options, lang=None):
    data, opts, errors = split_options(actor, data, options, lang)
    _, input_errors = validate_input(actor.input_schema, fill_secrets(actor, data), lang)
    if errors + input_errors:
        raise InputError(errors + input_errors)
    return data, opts


def save_task(storage: Storage, name: str, actor_name: str, input=None, options=None,
              lang=None) -> dict:
    """Create or replace a task. Raises TaskError (name, actor) or InputError (input)."""
    path = _path(storage, name)
    actor = get_actor(actor_name)
    if actor is None:
        raise TaskError(f"unknown actor: {actor_name}")
    data, opts = _check(actor, input or {}, options, lang)
    # a secret is never written to disk: a task run reads it from the field's env variable
    data = {k: v for k, v in data.items() if k not in secret_fields(actor.input_schema)}
    prev = load_task(storage, name) or {}
    task = {"name": name, "actId": actor.name, "input": data, "options": opts,
            "createdAt": prev.get("createdAt") or _now(), "modifiedAt": _now()}
    atomic_write(path, json.dumps(task, ensure_ascii=False, indent=2).encode("utf-8"))
    return task


def load_task(storage: Storage, name: str):
    path = _path(storage, name)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def list_tasks(storage: Storage) -> list:
    folder = storage.root / "tasks"
    if not folder.is_dir():
        return []
    out = []
    for p in sorted(folder.glob("*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except ValueError:
            continue
    return out


def delete_task(storage: Storage, name: str) -> bool:
    path = _path(storage, name)
    if not path.is_file():
        return False
    path.unlink()
    return True


def task_run_args(storage: Storage, name: str, input_override=None, options=None):
    """(actor name, merged input, merged options) for a run of the task. The override is
    merged over the saved input key by key; options given here win over the task's."""
    task = load_task(storage, name)
    if task is None:
        raise TaskError(f"unknown task: {name} (see `ww-scraper task list`)")
    if input_override is not None and not isinstance(input_override, dict):
        raise InputError(["input override must be a JSON object"])
    data = dict(task.get("input") or {}, **(input_override or {}))
    opts = dict(task.get("options") or {}, **{k: v for k, v in (options or {}).items() if v is not None})
    return task["actId"], data, opts


def run_task(storage: Storage, name: str, input_override=None, options=None, **kwargs) -> dict:
    """Run a saved task synchronously and return the final run record (with taskName)."""
    actor, data, opts = task_run_args(storage, name, input_override, options)
    return run_actor(actor, data, storage, options=opts, task_name=name, **kwargs)
