"""`ww-scraper schedule` only PRINTS a crontab line and a launchd plist. Installing either is
the operator's decision; ww-scraper never installs persistence by itself.

The target is an actor with an input, or a saved task (`--task NAME`). Besides 5-field
expressions, the macros @hourly, @daily (@midnight), @weekly, @monthly and @yearly
(@annually) are accepted. Times are the machine's local time; `--tz` adds a CRON_TZ line
for cron implementations that read it (launchd always uses local time)."""
from __future__ import annotations

import itertools
import re
import shlex
import sys
from pathlib import Path

_FIELDS = ("Minute", "Hour", "Day", "Month", "Weekday")
MACROS = {"@hourly": "0 * * * *", "@daily": "0 0 * * *", "@midnight": "0 0 * * *",
          "@weekly": "0 0 * * 0", "@monthly": "0 0 1 * *", "@yearly": "0 0 1 1 *",
          "@annually": "0 0 1 1 *"}


def expand_cron(cron: str) -> str:
    """A macro to its 5 fields; a 5-field expression unchanged; anything else is refused."""
    cron = " ".join(str(cron).split())
    if cron.startswith("@"):
        if cron.lower() not in MACROS:
            raise ValueError(f"unknown cron macro {cron!r} (use {', '.join(MACROS)})")
        return MACROS[cron.lower()]
    if len(cron.split()) != 5:
        raise ValueError("cron expression needs 5 fields, e.g. '0 6 * * 1', or a macro like @daily")
    return cron


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _plist_value(v, indent: int) -> list:
    pad = "\t" * indent
    if isinstance(v, bool):
        return [f"{pad}<{'true' if v else 'false'}/>"]
    if isinstance(v, int):
        return [f"{pad}<integer>{v}</integer>"]
    if isinstance(v, str):
        return [f"{pad}<string>{_esc(v)}</string>"]
    if isinstance(v, list):
        return [f"{pad}<array>"] + [ln for x in v for ln in _plist_value(x, indent + 1)] + [f"{pad}</array>"]
    if isinstance(v, dict):
        out = [f"{pad}<dict>"]
        for k, x in v.items():
            out.append(f"{pad}\t<key>{_esc(k)}</key>")
            out += _plist_value(x, indent + 1)
        return out + [f"{pad}</dict>"]
    raise TypeError(type(v))


def plist_dumps(obj: dict) -> str:
    """Minimal XML property-list writer (dict/list/str/int/bool). plistlib is avoided on
    purpose: it imports the expat parser even for writing."""
    head = ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n<plist version="1.0">\n')
    return head + "\n".join(_plist_value(obj, 0)) + "\n</plist>\n"


def _command(actor, input_json, workdir, python, task=None):
    storage = ["--storage", str(Path(workdir) / "storage")]
    if task:
        return [python, "-m", "ww_scraper", "task", "run", task, *storage]
    return [python, "-m", "ww_scraper", "run", actor, "--input", input_json, *storage]


def _label(actor, task=None):
    return (f"io.github.wwintegrations.ww-scraper.task.{task}" if task
            else f"io.github.wwintegrations.ww-scraper.{actor}")


def cron_line(actor: str, input_json: str, cron: str, workdir: str, python: str = None,
              log: str = None, task: str = None) -> str:
    cron = expand_cron(cron)
    python = python or sys.executable
    log = log or str(Path(workdir) / "storage" / "schedule.log")
    cmd = " ".join(shlex.quote(c) for c in _command(actor, input_json, workdir, python, task))
    line = f"{cron} cd {shlex.quote(workdir)} && {cmd} >> {shlex.quote(log)} 2>&1"
    return line.replace("%", "\\%")  # % is special in crontab


def _expand(field: str, name: str):
    if field == "*":
        return [None]
    vals = []
    for part in field.split(","):
        if not re.fullmatch(r"\d+", part):
            raise ValueError(f"launchd: field {name} supports only numbers, lists or '*' "
                             f"(got {field!r}); use the cron line instead")
        vals.append(int(part))
    return vals


def launchd_plist(actor: str, input_json: str, cron: str, workdir: str, python: str = None,
                  label: str = None, task: str = None) -> str:
    parts = expand_cron(cron).split()
    label = label or _label(actor, task)
    python = python or sys.executable
    plist = {
        "Label": label,
        "ProgramArguments": _command(actor, input_json, workdir, python, task),
        "WorkingDirectory": workdir,
        "StandardOutPath": str(Path(workdir) / "storage" / "schedule.log"),
        "StandardErrorPath": str(Path(workdir) / "storage" / "schedule.log"),
        "RunAtLoad": False,
    }
    m = re.fullmatch(r"\*/(\d+)", parts[0])
    if m and parts[1:] == ["*"] * 4:
        plist["StartInterval"] = int(m.group(1)) * 60
    else:
        combos = []
        for vals in itertools.product(*(_expand(f, n) for f, n in zip(parts, _FIELDS))):
            d = {n: v for n, v in zip(_FIELDS, vals) if v is not None}
            combos.append(d)
        plist["StartCalendarInterval"] = combos[0] if len(combos) == 1 else combos
    return plist_dumps(plist)


def render(actor, input_json, cron, workdir, python=None, task=None, tz=None) -> str:
    label = _label(actor, task)
    target = f"task {task}" if task else f"actor {actor}"
    out = [f"# {target}, schedule {cron!r} = {expand_cron(cron)!r}"]
    if tz:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"unknown time zone: {tz!r}") from None
        out += [f"# times are in {tz}; CRON_TZ is read by cronie and some other cron daemons,",
                "# not by macOS cron or launchd, which use the machine's local time", f"CRON_TZ={tz}"]
    else:
        out.append("# times are the machine's local time zone (pass --tz to name another one)")
    out += ["# crontab line (install yourself with `crontab -e`):",
            cron_line(actor, input_json, cron, workdir, python, task=task), ""]
    try:
        plist = launchd_plist(actor, input_json, cron, workdir, python, label, task=task)
        out += [f"# launchd plist (save as ~/Library/LaunchAgents/{label}.plist, then",
                f"#   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/{label}.plist):",
                plist]
    except ValueError as e:
        out.append(f"# launchd: {e}")
    out.append("# Nothing was installed. Scheduling is the operator's decision.")
    return "\n".join(out)
