# Limitations

What cfgtrack does not do, and what to do about it.

**The rules are best effort.** A profile knows how Cisco IOS, Junos and RouterOS print the lines that change by
themselves and the passwords they contain. A firmware that prints other volatile lines makes a commit on every backup
(`cfgtrack noise` proposes the patterns). A secret in a format nobody wrote a rule for is **stored in clear text**:
keep the repository private, add `redact` patterns, run `cfgtrack verify`. The measured 0 of 374 holds for the secrets
the generator writes, in the shapes the rules were written for.

**A partial loss that keeps the last line looks like a real removal.** A session that dies halfway and still ends with
`end`, or a device that returns only some interfaces, produces a complete-looking configuration. The checks catch an
empty answer, an error message, a configuration that does not end like one and, with `shrink_guard`, one that lost more
than half of its lines. Between a real removal of 30 % of the configuration and a broken fetch that lost 30 % there is
nothing to tell them apart; it is stored, the next good fetch puts the lines back, and the history shows both. The report
shows how large each change was.

**The repository cannot restore a device.** Passwords and keys are replaced by tags, so what is stored is a record of
what changed, not a backup you can paste into a console. Keep real backups of the devices by the means the vendor gives.

**Losing the key changes every tag once.** The key is in `cfgtrack.key`, outside the repository. If it is lost a new one is
made and the next backup commits every line with a secret once. Back the key up separately.

**A moved block is a removal plus an addition.** The order inside a section is meaningful (access lists, route maps), so
a block that moved is reported as removed and added. Entries that devices print in a different order on every run
(some firewalls print object lists unordered) show as noise; sort them in a `command` fetch.

**Time is the time of the backup.** A commit is dated when cfgtrack noticed the change, not when someone made it; the
device's own log tells you who. The diff of a commit is against the previous *backup*, so two edits between backups appear together.

**SSH.** One-shot (`exec`) and interactive (`shell`) sessions are supported; telnet is not. In shell mode the prompt is
recognised by a pattern (`prompt`) and by silence after it, which costs about a second per device; a device that pages
needs the command to switch paging off in `before`. Devices that ask for an enable password or a second factor are best
handled with a small script used as a `command` fetch.

**Scale.** Each stored change runs a few `git` processes and devices are fetched in parallel threads: hundreds of devices,
daily or hourly, is the target. It is not a streaming system for thousands of devices every minute.

**The evaluation is synthetic.** The simulated configurations are printed the way IOS does it, which is also what the IOS
profile was written for, so 100 % there is a statement about the machinery, not about every device. Junos and RouterOS
are covered by unit tests on sample lines, not by the simulation.
