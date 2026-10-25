"""`lustro schedule` only PRINTS a crontab line and a launchd plist. Installing either is
the operator's decision; Lustro never installs persistence by itself."""
from __future__ import annotations

import itertools
import re
import shlex
import sys
from pathlib import Path

_FIELDS = ("Minute", "Hour", "Day", "Month", "Weekday")


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


def _command(actor, input_json, workdir, python):
    return [python, "-m", "lustro", "run", actor, "--input", input_json,
            "--storage", str(Path(workdir) / "storage")]


def cron_line(actor: str, input_json: str, cron: str, workdir: str, python: str = None,
              log: str = None) -> str:
    if len(cron.split()) != 5:
        raise ValueError("cron expression needs 5 fields, e.g. '0 6 * * 1'")
    python = python or sys.executable
    log = log or str(Path(workdir) / "storage" / "schedule.log")
    cmd = " ".join(shlex.quote(c) for c in _command(actor, input_json, workdir, python))
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
                  label: str = None) -> str:
    parts = cron.split()
    if len(parts) != 5:
        raise ValueError("cron expression needs 5 fields")
    label = label or f"io.github.wwintegrations.lustro.{actor}"
    python = python or sys.executable
    plist = {
        "Label": label,
        "ProgramArguments": _command(actor, input_json, workdir, python),
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


def render(actor, input_json, cron, workdir, python=None) -> str:
    label = f"io.github.wwintegrations.lustro.{actor}"
    out = ["# crontab line (install yourself with `crontab -e`):",
           cron_line(actor, input_json, cron, workdir, python), ""]
    try:
        plist = launchd_plist(actor, input_json, cron, workdir, python, label)
        out += [f"# launchd plist (save as ~/Library/LaunchAgents/{label}.plist, then",
                f"#   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/{label}.plist):",
                plist]
    except ValueError as e:
        out.append(f"# launchd: {e}")
    out.append("# Nothing was installed. Scheduling is the operator's decision.")
    return "\n".join(out)
