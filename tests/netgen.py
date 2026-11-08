"""Synthetic switch/router configurations with known changes, to measure the tool against ground truth.

The generator keeps a model of the device and prints it the way an IOS-like box does: with the lines that change on
every run (timestamps, byte counts, clock period) and with real passwords. A mutation edits the model and says exactly
which lines it added and removed, computed here by hand and independently of the diff code.
"""

from __future__ import annotations

import random
import string
from dataclasses import dataclass, field

ChangeKey = tuple[str, tuple[str, ...], str]  # (added|removed, path, line)
SECRET_TAG_RE = r"<secret:[0-9a-f]{10}>"


def _word(rng: random.Random, n: int = 8) -> str:
    return "".join(rng.choice(string.ascii_lowercase) for _ in range(n))


def _secret(rng: random.Random) -> str:
    return "".join(rng.choice(string.ascii_letters + string.digits) for _ in range(14))


@dataclass
class Iface:
    name: str
    description: str
    vlan: int | None  # access port when set
    ip: str | None  # routed port when set
    shutdown: bool = False


@dataclass
class Model:
    name: str
    ifaces: list[Iface]
    vlans: dict[int, str]
    acls: dict[str, list[str]]
    users: dict[str, str]
    snmp: str
    tacacs: str
    enable: str
    routes: list[str]
    vty_password: str
    secrets_seen: set[str] = field(default_factory=set)

    def all_secrets(self) -> set[str]:
        return {self.snmp, self.tacacs, self.enable, self.vty_password, *self.users.values()}


def make_model(rng: random.Random, name: str, ports: int = 24, acl_entries: int = 12) -> Model:
    ifaces = []
    for i in range(1, ports + 1):
        routed = i <= 4
        ifaces.append(
            Iface(
                f"GigabitEthernet0/{i}",
                f"{rng.choice(['user', 'server', 'ap', 'printer', 'uplink', 'voip'])}-{_word(rng, 5)}",
                None if routed else rng.choice([10, 20, 30, 40]),
                f"10.{rng.randint(0, 250)}.{rng.randint(0, 250)}.1 255.255.255.0" if routed else None,
                shutdown=rng.random() < 0.15,
            )
        )
    acls = {}
    for a in range(3):
        entries = [
            f"permit tcp 10.{rng.randint(0, 250)}.{rng.randint(0, 250)}.0 0.0.0.255 host 10.9.{rng.randint(0, 250)}.{n + 1} eq {rng.choice([22, 80, 443, 3389, 5432])}"
            for n in range(acl_entries)
        ]
        entries.append("deny ip any any log")
        acls[f"ACL-{a}"] = entries
    return Model(
        name=name,
        ifaces=ifaces,
        vlans={v: f"vlan-{_word(rng, 4)}" for v in (10, 20, 30, 40)},
        acls=acls,
        users={"admin": _secret(rng), "backup": _secret(rng), "noc": _secret(rng)},
        snmp=_secret(rng),
        tacacs=_secret(rng),
        enable=_secret(rng),
        routes=[f"ip route 10.{rng.randint(0, 250)}.0.0 255.255.0.0 10.0.0.{rng.randint(2, 250)}" for _ in range(6)],
        vty_password=_secret(rng),
    )


def render(m: Model, rng: random.Random) -> str:
    """What `show running-config` prints right now: the model plus the lines that differ on every run."""
    hh, mm, ss = rng.randint(0, 23), rng.randint(0, 59), rng.randint(0, 59)
    body = [
        "!",
        f"! Last configuration change at {hh:02}:{mm:02}:{ss:02} UTC Mon Nov {rng.randint(1, 28)} 2026 by admin",
        f"! NVRAM config last updated at {hh:02}:{mm:02}:{ss:02} UTC Mon Nov {rng.randint(1, 28)} 2026 by admin",
        "!",
        "version 15.2",
        f"hostname {m.name}",
        "!",
        f"enable secret 9 {m.enable}",
        "!",
    ]
    for u, s in m.users.items():
        body.append(f"username {u} privilege 15 secret 9 {s}")
    body += ["!"]
    for v, n in m.vlans.items():
        body += [f"vlan {v}", f" name {n}"]
    body.append("!")
    for i in m.ifaces:
        body.append(f"interface {i.name}")
        body.append(f" description {i.description}")
        if i.vlan is not None:
            body += [" switchport mode access", f" switchport access vlan {i.vlan}"]
        if i.ip:
            body.append(f" ip address {i.ip}")
        if i.shutdown:
            body.append(" shutdown")
        body.append("!")
    for acl, entries in m.acls.items():
        body.append(f"ip access-list extended {acl}")
        body += [f" {e}" for e in entries]
        body.append("!")
    body += list(m.routes)
    body += [
        f"snmp-server community {m.snmp} RO",
        f"tacacs-server key 7 {m.tacacs}",
        f"ntp clock-period {rng.randint(17_000_000, 18_000_000)}",
        "line vty 0 4",
        f" password 7 {m.vty_password}",
        " login",
        "!",
        "end",
    ]
    text = "\n".join(body)
    return f"Building configuration...\n\nCurrent configuration : {len(text) + rng.randint(0, 99)} bytes\n{text}\n"


# ---- mutations ----
# Each takes the model and rng, edits the model, and returns the exact set of lines that must be reported.


def _path_if(i: Iface) -> tuple[str, ...]:
    return (f"interface {i.name}",)


def m_description(m: Model, rng: random.Random) -> list[ChangeKey]:
    i = rng.choice(m.ifaces)
    old, i.description = i.description, f"moved-{_word(rng, 6)}"
    return [("removed", _path_if(i), f"description {old}"), ("added", _path_if(i), f"description {i.description}")]


def m_ip(m: Model, rng: random.Random) -> list[ChangeKey]:
    i = rng.choice([x for x in m.ifaces if x.ip])
    assert i.ip
    old, i.ip = i.ip, f"172.16.{rng.randint(0, 250)}.1 255.255.255.0"
    return [("removed", _path_if(i), f"ip address {old}"), ("added", _path_if(i), f"ip address {i.ip}")]


def m_shutdown(m: Model, rng: random.Random) -> list[ChangeKey]:
    i = rng.choice(m.ifaces)
    i.shutdown = not i.shutdown
    return [("added" if i.shutdown else "removed", _path_if(i), "shutdown")]


def m_acl_add(m: Model, rng: random.Random) -> list[ChangeKey]:
    acl = rng.choice(list(m.acls))
    line = f"permit udp 10.{rng.randint(0, 250)}.0.0 0.0.255.255 any eq {rng.randint(1000, 9000)}"
    m.acls[acl].insert(len(m.acls[acl]) - 1, line)  # before the final deny
    return [("added", (f"ip access-list extended {acl}",), line)]


def m_acl_remove(m: Model, rng: random.Random) -> list[ChangeKey]:
    acl = rng.choice(list(m.acls))
    line = m.acls[acl].pop(rng.randrange(len(m.acls[acl]) - 1))
    return [("removed", (f"ip access-list extended {acl}",), line)]


def m_vlan_add(m: Model, rng: random.Random) -> list[ChangeKey]:
    v = max(m.vlans) + rng.randint(1, 9)
    m.vlans[v] = f"vlan-{_word(rng, 4)}"
    return [("added", (), f"vlan {v}"), ("added", (f"vlan {v}",), f"name {m.vlans[v]}")]


def m_user_secret(m: Model, rng: random.Random) -> list[ChangeKey]:
    u = rng.choice(list(m.users))
    m.users[u] = _secret(rng)
    line = f"username {u} privilege 15 secret 9 <secret:*>"
    return [("removed", (), line), ("added", (), line)]


def m_snmp(m: Model, rng: random.Random) -> list[ChangeKey]:
    m.snmp = _secret(rng)
    line = "snmp-server community <secret:*> RO"
    return [("removed", (), line), ("added", (), line)]


def m_route(m: Model, rng: random.Random) -> list[ChangeKey]:
    r = f"ip route 192.168.{rng.randint(0, 250)}.0 255.255.255.0 10.0.0.{rng.randint(2, 250)}"
    m.routes.append(r)
    return [("added", (), r)]


# a reorder has no exact expected set (which of the moved lines is reported is not meaningful): checked loosely
def m_acl_swap(m: Model, rng: random.Random) -> list[ChangeKey]:
    acl = rng.choice(list(m.acls))
    e = m.acls[acl]
    k = rng.randrange(len(e) - 2)
    e[k], e[k + 1] = e[k + 1], e[k]
    return [("reorder", (f"ip access-list extended {acl}",), "")]


MUTATIONS = {
    "description": m_description,
    "ip": m_ip,
    "shutdown": m_shutdown,
    "acl_add": m_acl_add,
    "acl_remove": m_acl_remove,
    "vlan_add": m_vlan_add,
    "user_secret": m_user_secret,
    "snmp": m_snmp,
    "route": m_route,
    "acl_swap": m_acl_swap,
}


def mutate(m: Model, rng: random.Random, n: int) -> tuple[list[str], list[ChangeKey]]:
    """Apply n different kinds of change; returns their names and the changes that must be reported."""
    kinds = rng.sample(sorted(MUTATIONS), n)
    acl_kinds = [k for k in kinds if k.startswith("acl_")]
    kinds = [k for k in kinds if not k.startswith("acl_")] + acl_kinds[:1]  # two edits of one ACL can cancel out
    expected: list[ChangeKey] = []
    for k in kinds:
        expected += MUTATIONS[k](m, rng)
    return kinds, expected


# ---- transient faults: what a flaky session hands back instead of a configuration ----


def fault(text: str, rng: random.Random, kind: str) -> str:
    lines = text.splitlines()
    if kind == "truncated":
        return "\n".join(lines[: rng.randint(len(lines) // 5, len(lines) * 4 // 5)]) + "\n"
    if kind == "empty":
        return ""
    if kind == "error":
        return "% Invalid input detected at '^' marker.\n"
    if kind == "denied":
        return "Permission denied, please try again.\n"
    if kind == "wrong_context":  # a complete-looking but tiny configuration (the wrong VRF, a login banner only)
        return "\n".join([lines[0], "hostname " + "x" * 5, "end"]) + "\n"
    raise ValueError(kind)


FAULTS = ("truncated", "empty", "error", "denied", "wrong_context")
