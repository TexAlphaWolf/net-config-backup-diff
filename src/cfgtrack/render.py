"""Reports: what changed on which device and when, as Markdown, JSON or one self-contained HTML page."""

from __future__ import annotations

import html
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from cfgtrack.config import Config
from cfgtrack.diffing import Change, diff_text, format_changes, summarize
from cfgtrack.runner import State
from cfgtrack.store import Repo


@dataclass(frozen=True)
class Entry:
    device: str
    commit: str
    when: datetime
    subject: str
    changes: list[Change]
    added: int
    removed: int


def is_initial(e: Entry) -> bool:
    return ": initial backup (" in e.subject


def build_report(repo: Repo, styles: dict[str, str], since: datetime | None) -> list[Entry]:
    """Every commit since `since` (all history if None), oldest first, with the structural diff against its parent."""
    entries = []
    for name in repo.devices():
        style = styles.get(name, "indent")
        for c in repo.log(name, since=since):
            new = repo.read(name, c.id) or ""
            old = repo.read(name, f"{c.id}~1") or ""
            changes = diff_text(old, new, style) if old else []
            s = summarize(changes)
            entries.append(Entry(name, c.id, c.when, c.subject, changes, s.added, s.removed))
    entries.sort(key=lambda e: (e.when, e.device))
    return entries


def markdown(entries: list[Entry], since: datetime | None) -> str:
    title = f"# Configuration changes since {since:%Y-%m-%d %H:%M} UTC" if since else "# Configuration changes"
    out = [title, ""]
    real = [e for e in entries if not is_initial(e)]
    first = [e for e in entries if is_initial(e)]
    if not entries:
        return "\n".join([*out, "No changes.", ""])
    out.append(
        f"{len(real)} change(s) on {len({e.device for e in real})} device(s); "
        f"{len(first)} initial backup(s). Lines: +{sum(e.added for e in real)} -{sum(e.removed for e in real)}."
    )
    out.append("")
    for e in entries:
        out.append(f"## {e.device} - {e.when:%Y-%m-%d %H:%M} ({e.commit})")
        out.append("")
        if e.changes:
            out += ["```diff", format_changes(e.changes), "```", ""]
        else:
            out += [e.subject.split(": ", 1)[-1], ""]
    return "\n".join(out)


def to_json(entries: list[Entry]) -> str:
    return json.dumps(
        [
            {
                "device": e.device,
                "commit": e.commit,
                "when": e.when.isoformat(),
                "subject": e.subject,
                "added": e.added,
                "removed": e.removed,
                "changes": [c.as_dict() for c in e.changes],
            }
            for e in entries
        ],
        indent=2,
    )


CSS = (
    ":root{--bg:#fff;--fg:#1c2430;--muted:#5b6778;--card:#f5f7fa;--line:#dde3ea;--add:#e6f6ea;--addfg:#136f2e;"
    "--del:#fdecec;--delfg:#a12626}@media(prefers-color-scheme:dark){:root{--bg:#10151c;--fg:#e6ebf2;--muted:#9aa6b6;"
    "--card:#18202b;--line:#2a3441;--add:#12301c;--addfg:#6fdc8c;--del:#3a1a1a;--delfg:#ff9b9b}}"
    "body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}main{max-width:60rem;"
    "margin:0 auto;padding:1.5rem 1rem}h1{font-size:1.4rem;margin:.2rem 0 .3rem}h2{font-size:1.02rem;margin:1.6rem 0 .4rem}"
    ".muted{color:var(--muted)}.dev{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:.6rem .9rem;"
    "margin:.8rem 0}pre{margin:.4rem 0 0;font:13px/1.45 ui-monospace,Menlo,Consolas,monospace;overflow-x:auto}"
    "pre span{display:block;white-space:pre}.a{background:var(--add);color:var(--addfg)}.d{background:var(--del);"
    "color:var(--delfg)}.c{color:var(--muted)}table{border-collapse:collapse;width:100%}th,td{text-align:left;"
    "padding:4px 8px;border-bottom:1px solid var(--line)}th{color:var(--muted);font-weight:600}"
)


def _diff_html(changes: list[Change]) -> str:
    rows = []
    for line in format_changes(changes, limit=120).splitlines():
        cls = {"+": "a", "-": "d"}.get(line[:1], "c")
        rows.append(f'<span class="{cls}">{html.escape(line)}</span>')
    return "<pre>" + "".join(rows) + "</pre>"


def html_page(entries: list[Entry], since: datetime | None) -> str:
    e = html.escape
    real = [x for x in entries if not is_initial(x)]
    body = ["<h1>Network configuration changes</h1>"]
    span = f"since {since:%Y-%m-%d %H:%M} UTC" if since else "all history"
    body.append(
        f'<p class="muted">{e(span)}: {len(real)} change(s) on {len({x.device for x in real})} device(s), '
        f"+{sum(x.added for x in real)} -{sum(x.removed for x in real)} lines.</p>"
    )
    if not entries:
        body.append("<p>No changes.</p>")
    for x in entries:
        body.append(
            f'<div class="dev"><strong>{e(x.device)}</strong> <span class="muted">{x.when:%Y-%m-%d %H:%M} UTC '
            f"· {e(x.commit)} · {e(x.subject.split(': ', 1)[-1])}</span>"
        )
        if x.changes:
            body.append(_diff_html(x.changes))
        body.append("</div>")
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>Configuration changes</title><style>{CSS}</style></head><body><main>{''.join(body)}</main></body></html>"
    )


def status_rows(cfg: Config, state: State, now: datetime | None = None) -> list[dict[str, object]]:
    now = now or datetime.now(UTC)
    rows = []
    for d in cfg.devices:
        st = state.devices.get(d.name, {})
        last_ok = str(st.get("last_ok") or "")
        age = (now - datetime.fromisoformat(last_ok)).total_seconds() if last_ok else None
        stale = age is None or age > cfg.stale_after
        rows.append(
            {
                "device": d.name,
                "last_ok": last_ok,
                "last_change": str(st.get("last_change") or ""),
                "failures": int(str(st.get("failures", 0))),
                "error": str(st.get("last_error") or ""),
                "stale": stale,
            }
        )
    return rows


def status_text(rows: list[dict[str, object]]) -> str:
    if not rows:
        return "no devices configured"
    w = max(len(str(r["device"])) for r in rows)
    out = [f"{'device'.ljust(w)}  state   last backup (UTC)    last change (UTC)    failures  last error"]
    for r in rows:
        state = "STALE" if r["stale"] else "ok"
        ok = str(r["last_ok"])[:16].replace("T", " ") or "never"
        ch = str(r["last_change"])[:16].replace("T", " ") or "-"
        out.append(
            f"{str(r['device']).ljust(w)}  {state:<6}  {ok:<19}  {ch:<19}  {r['failures']:<8}  {r['error']}".rstrip()
        )
    return "\n".join(out)
