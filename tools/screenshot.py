"""Render docs/demo.png: the HTML report of a week on a SYNTHETIC network (no real device is needed to show the page).

pip install -e ".[dev]" && playwright install chromium
python tools/screenshot.py
"""

from __future__ import annotations

import random
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "src"))

from playwright.sync_api import sync_playwright  # noqa: E402

import netgen  # noqa: E402
from cfgtrack.config import Config, Device  # noqa: E402
from cfgtrack.profiles import IOS  # noqa: E402
from cfgtrack.render import build_report, html_page  # noqa: E402
from cfgtrack.runner import State, run_backup  # noqa: E402
from cfgtrack.store import Repo  # noqa: E402

OUT = ROOT / "docs" / "demo.png"
T0 = datetime(2026, 10, 26, 3, 0, tzinfo=UTC)
KEY = bytes(range(32))

# what happens, day by day, to which device: (day, device, mutation names). Everything else is noise only.
SCRIPT = [
    (2, "core-sw1", ["description"]),
    (3, "dist-sw2", ["acl_add", "shutdown"]),
    (4, "core-sw1", ["vlan_add", "user_secret"]),
    (5, "edge-rtr", ["route", "snmp"]),
    (6, "dist-sw2", ["ip"]),
]


def main() -> None:
    rng = random.Random(21)
    names = ["core-sw1", "dist-sw2", "edge-rtr"]
    models = {n: netgen.make_model(rng, n, ports=12, acl_entries=6) for n in names}
    mutations = {k: netgen.MUTATIONS[k] for k in netgen.MUTATIONS}
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        cfg = Config(
            store=base / "backups",
            key_file=base / "k",
            devices=tuple(Device(name=n, profile=IOS, fetch="file") for n in names),
        )
        repo, state = Repo(cfg.store), State(base / "s.json")
        for day in range(8):
            for d, name, kinds in SCRIPT:
                if d == day:
                    for k in kinds:
                        mutations[k](models[name], rng)
            raw = {n: netgen.render(models[n], rng) for n in names}
            run_backup(
                cfg, repo, state, KEY, now=T0 + timedelta(days=day), fetcher=lambda dev, env, raw=raw: raw[dev.name]
            )
        since = T0 + timedelta(days=1)
        entries = [e for e in build_report(repo, {n: "indent" for n in names}, since) if e.changes]
        page = base / "report.html"
        page.write_text(html_page(entries, since), encoding="utf-8")
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            shot = browser.new_page(viewport={"width": 860, "height": 900}, device_scale_factor=2)
            shot.goto(page.as_uri())
            shot.screenshot(path=str(OUT), full_page=True)
            browser.close()
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
