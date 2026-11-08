from __future__ import annotations

import random
from collections import Counter

import pytest

from cfgtrack.diffing import Change, diff_text, format_changes, parse, summarize

OLD = """\
hostname sw1
interface Gi0/1
 description uplink
 shutdown
interface Gi0/2
 description printer
ip access-list extended MGMT
 permit tcp any any eq 22
 deny ip any any
"""


def keys(changes: list[Change]) -> set[tuple[str, tuple[str, ...], str]]:
    return {(c.kind, c.path, c.line) for c in changes}


def test_identical_texts_have_no_changes() -> None:
    assert diff_text(OLD, OLD, "indent") == []


def test_a_changed_line_is_reported_under_its_section() -> None:
    new = OLD.replace("description uplink", "description uplink-core")
    assert keys(diff_text(OLD, new, "indent")) == {
        ("removed", ("interface Gi0/1",), "description uplink"),
        ("added", ("interface Gi0/1",), "description uplink-core"),
    }


def test_a_new_section_is_reported_line_by_line() -> None:
    new = OLD + "vlan 20\n name lab\n"
    assert keys(diff_text(OLD, new, "indent")) == {("added", (), "vlan 20"), ("added", ("vlan 20",), "name lab")}


def test_a_removed_section() -> None:
    new = OLD.replace("interface Gi0/2\n description printer\n", "")
    assert keys(diff_text(OLD, new, "indent")) == {
        ("removed", (), "interface Gi0/2"),
        ("removed", ("interface Gi0/2",), "description printer"),
    }


def test_order_matters_in_a_section() -> None:
    new = OLD.replace(" permit tcp any any eq 22\n deny ip any any\n", " deny ip any any\n permit tcp any any eq 22\n")
    ch = diff_text(OLD, new, "indent")
    assert ch and {c.path for c in ch} == {("ip access-list extended MGMT",)}


def test_the_same_line_in_two_sections_is_told_apart() -> None:
    old = "interface A\n shutdown\ninterface B\n shutdown\n"
    new = "interface A\n shutdown\ninterface B\n"
    assert keys(diff_text(old, new, "indent")) == {("removed", ("interface B",), "shutdown")}


def test_format_and_summary() -> None:
    new = OLD.replace("description uplink", "description uplink-core") + "vlan 20\n name lab\n"
    ch = diff_text(OLD, new, "indent")
    text = format_changes(ch)
    assert "  interface Gi0/1\n-   description uplink\n+   description uplink-core" in text
    assert "+ vlan 20\n+   name lab" in text  # the header is itself a change: not printed twice
    s = summarize(ch)
    assert (s.added, s.removed, s.sections) == (3, 1, ("interface Gi0/1", "vlan 20"))
    assert "more changes" in format_changes(ch, limit=2)


def test_junos_braces() -> None:
    old = "system {\n    host-name r1;\n    services {\n        ssh;\n    }\n}\n"
    new = old.replace("ssh;", "ssh;\n        netconf;")
    assert keys(diff_text(old, new, "braces")) == {("added", ("system", "services"), "netconf;")}
    tree = parse(old, "braces")
    assert tree[0].text == "system" and tree[0].children[1].text == "services"


def test_routeros_sections_and_continuations() -> None:
    old = "/ip address\nadd address=10.0.0.1/24 interface=ether1\n/ip firewall filter\nadd chain=input \\\n    action=accept\n"
    new = old.replace("action=accept", "action=drop")
    assert keys(diff_text(old, new, "sections")) == {
        ("removed", ("/ip firewall filter",), "add chain=input action=accept"),
        ("added", ("/ip firewall filter",), "add chain=input action=drop"),
    }


def test_unknown_style() -> None:
    with pytest.raises(ValueError, match="unknown style"):
        parse("x", "yaml")


def test_blank_lines_and_tabs_do_not_matter() -> None:
    assert diff_text("a\n b\n", "a\n\n\tb\n".replace("\t", " "), "indent") == []


def _random_config(rng: random.Random) -> str:
    lines = []
    for _s in range(rng.randint(1, 6)):
        lines.append(f"section {rng.randint(0, 9)}")
        for _ in range(rng.randint(0, 5)):
            lines.append(f" item {rng.randint(0, 9)}")
            if rng.random() < 0.3:
                lines.append(f"  sub {rng.randint(0, 3)}")
    return "\n".join(lines) + "\n"


def _flat(text: str) -> Counter[tuple[tuple[str, ...], str]]:
    out: Counter[tuple[tuple[str, ...], str]] = Counter()

    def walk(nodes: list, path: tuple[str, ...]) -> None:  # type: ignore[type-arg]
        for n in nodes:
            out[(path, n.text)] += 1
            walk(n.children, (*path, n.text))

    walk(parse(text, "indent"), ())
    return out


def test_net_effect_of_the_diff_is_the_difference_of_the_two_configurations() -> None:
    """Whatever the diff reports, adding its additions and removing its removals must turn old into new."""
    rng = random.Random(7)
    for _ in range(300):
        a, b = _random_config(rng), _random_config(rng)
        ch = diff_text(a, b, "indent")
        net: Counter[tuple[tuple[str, ...], str]] = Counter()
        for c in ch:
            net[(c.path, c.line)] += 1 if c.kind == "added" else -1
        want = _flat(b)
        want.subtract(_flat(a))
        assert {k: v for k, v in net.items() if v} == {k: v for k, v in want.items() if v}
