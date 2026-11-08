# Contributing

```bash
pip install -e ".[dev]"
ruff check . && ruff format --check . && mypy && pytest -n auto --cov
```

Git must be installed. The SSH code is tested against a real SSH server on loopback (`tests/fakedevice.py`, paramiko's
server side), not against mocks; keep it that way. New secret or noise rules need a line in `tests/test_profiles.py`
that shows what they match **and** a line they must leave alone.

Claims in the docs are checked against `docs/report.json` by `tests/test_docs.py`. Re-run `python tools/evaluate.py
--out docs/report.json` and update the text when the numbers change.
