# Evaluation

Produced by `python tools/evaluate.py` and stored in `report.json` (machine: Linux x86_64, Python 3.13.16).

**What this is and is not.** There is no fleet of real devices in a test environment, so every number comes from a
situation where the truth is known: a real SSH server on loopback that answers like a router, and a simulated network
whose configurations are edited by a generator that records exactly which lines it added and removed. The expected
result of every run is therefore written down *before* the tool sees the configuration, independently of the tool's own
diff code. It shows that the machinery does what it claims. It does not show that the built-in rules cover every real
device: the noise and the passwords in the simulated configurations are written the way Cisco IOS prints them, which is
also what the IOS profile was written for. A device with other noise needs `ignore` patterns (section 3 shows what
happens, and how `cfgtrack noise` finds them).

## 1. Is what is fetched what the device printed?

The same 40 fetches of a 202-line configuration through each way of getting it, compared with what the device
holds after normalisation. SSH goes through a genuine handshake with a pinned host key (paramiko's server side on loopback).

| way | identical | median | 95th percentile |
|---|---|---|---|
| ssh exec | 40 of 40 | 147.14 ms | 151.35 ms |
| ssh shell | 40 of 40 | 964.91 ms | 1006.28 ms |
| http | 40 of 40 | 0.85 ms | 2.1 ms |
| command | 40 of 40 | 11.14 ms | 12.42 ms |
| file | 40 of 40 | 0.02 ms | 0.09 ms |

The interactive shell is about a second slower than a one-shot command because it waits for the device to be quiet after
each prompt (so that a line ending in `#` in the middle of the output is not taken for the prompt). Use `exec` where the
device allows it. On loopback there is no network latency: on a real device the time is the device's.

## 2. A simulated network with known changes

40 devices, 60 days, one backup per device per day: 2400 rounds. Every day each device is, with probability
12%, really reconfigured (1 to 3 of ten kinds of edit, listed below) and, with probability 3%, answers with garbage
instead of a configuration (cut off, empty, an error message, "permission denied", or a complete-looking but tiny
configuration). Every fetch also carries the noise a switch really prints (timestamps, byte counts, a clock counter), and
the configurations contain 374 distinct passwords, keys and community strings over the run. A baseline that "commits whatever the
device prints" ran next to cfgtrack on the same fetches.

| | cfgtrack | baseline |
|---|---|---|
| real changes found | 271 of 271 | every fetch is a commit, real or not |
| commits on 2022 days with no change | 0 | 2022 |
| commits in total (including the repository's first) | 312 | 2401 |
| lines the baseline's diff shows on a day with no change | - | 8 on average |
| fetches that were not configurations, stored | 0 of 67 | 67 of 67 |
| passwords found in the history | 0 of 374 | 374 of 374 |
| repository size after `git gc` | 207 KiB | 1190 KiB |

**Is the report exact?** For the 232 change events that were not a reordering, the set of reported lines (kind, section, text) was
identical to the injected set in 232 of 232: not a line more, not a line less, each under the right section. A rotated password is reported as one
removed and one added line with different tags (the value itself appears nowhere). The 39 reorderings of an access list were all reported as a
change in that list and nothing else (39 of 39); a moved entry shows as removed and added again, because in an access list the order is the meaning.

| kind of edit | events | found | reported exactly |
|---|---|---|---|
| description | 54 | 54 | 54 |
| ip | 42 | 42 | 42 |
| shutdown | 52 | 52 | 52 |
| acl_add | 47 | 47 | 47 |
| acl_remove | 51 | 51 | 51 |
| vlan_add | 53 | 53 | 53 |
| user_secret | 51 | 51 | 51 |
| snmp | 43 | 43 | 43 |
| route | 44 | 44 | 44 |
| acl_swap | 39 | 39 | 39 |

(An event can contain several kinds, so the rows add up to more than 271.)

**Fetches that are not configurations**, by kind of fault:

| fault | injected | held back |
|---|---|---|
| truncated | 14 | 14 |
| empty | 14 | 14 |
| error | 14 | 14 |
| denied | 11 | 11 |
| wrong_context | 14 | 14 |

Held back means: reported as an error, the stored configuration left alone, the next good fetch continues from it. The
"complete-looking but tiny" case is caught by the shrink guard (a new configuration with fewer than half of the old
lines). A fault that deletes a smaller part and keeps the last line is **not** distinguishable from a real removal and
would be stored; see `limitations.md`.

## 3. A device whose noise nobody wrote a rule for

A device printing two volatile lines the profile does not know (`system-uptime N seconds`, `last-sync: date time`):
over 20 days it made 20 commits and no real change. `cfgtrack noise` looked at that history and suggested
2 patterns, exactly those two shapes; with them added to the device, 18 quiet days made 0 commits, a later real edit was still found, and
none of the suggested patterns hides a line of the real configuration.

## 4. Cost

| lines | normalise | diff with 20 edited lines |
|---|---|---|
| 1,160 | 3.7 ms | 2.4 ms |
| 10,158 | 27.5 ms | 24.2 ms |
| 50,160 | 140.4 ms | 160.5 ms |

A pass over 100 devices (fetch excluded, as it depends on the network) took 1.53 s the first time, when every device is committed,
and 0.25 s when nothing had changed (2.5 ms per device). Each stored change is one `git` commit (a few processes), so the
tool is meant for hundreds of devices, not for hundreds of thousands.
