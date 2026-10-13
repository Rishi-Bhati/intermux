# Contributing to InterMux

Thanks for helping out. This guide covers the development setup, the tests and the code rules.

## Development Setup

InterMux has no runtime dependencies beyond Python 3.9+ and the standard library. You need the system tools listed in the README (`iproute2`, `iptables`, `procps`, `util-linux`, `polkit`) to try things end to end.

```bash
git clone https://github.com/Rishi-Bhati/intermux.git
cd intermux

python3 -m venv .venv
source .venv/bin/activate

# Optional: install the package in editable mode to get the `intermux` command
pip install -e .
```

Run from source at any time with `python3 -m intermux`.

## Running the Tests

Unit tests use only temporary directories and pure functions. They do not change system state and do not need root:

```bash
python3 -m compileall -q intermux tests
python3 -m unittest discover -s tests -t . -v
```

### Integration Test (Optional)

The namespace integration test creates a dummy interface, network namespaces and routing rules on the machine it runs on. Run it only in a virtual machine or another disposable system:

```bash
sudo -E env INTERMUX_INTEGRATION=1 python3 -m unittest tests.test_netns_integration -v
```

It is skipped unless `INTERMUX_INTEGRATION=1` is set and the process runs as root. Its teardown removes everything it created.

## Code Rules

- **Python 3.9+.** Start each module with `from __future__ import annotations`.
- **Standard library only.** Do not add third-party imports to the runtime package.
- **Absolute imports** only, for example `from intermux.system.commands import run`.
- **No `shell=True`, no `os.system`.** Every command is a list of strings.
- **No `print()` in library code.** Only `cli.py` and the `ui/` package print. Other modules log with `logging.getLogger("intermux.<module>")`.
- **Raise InterMux errors.** Use the exception types in `intermux/errors.py`. Never raise a bare `Exception`.
- **Keep the privileged side small.** Anything that runs as root belongs in the helper and must be validated in `intermux/helper/protocol.py`.
- **Docstrings** are one or two lines per public function. Add comments only where the reason is not obvious.
- **Plain text in code.** No emojis in source, log messages or command output.

## Making a Change

1. Open an issue first for larger changes, so the approach can be discussed.
2. Create a branch from `main`.
3. Keep commits focused. Use short prefixes such as `feat:`, `fix:`, `docs:` or `chore:`.
4. Add or update tests for the behavior you change.
5. Make sure `compileall` and the unit tests pass before opening a pull request.
6. Describe what changed and how you tested it. For changes that touch networking, say which distribution and session type you used.

Changes to the Windows edition live under `windows/` and follow that directory's own README.

## Reporting Security Issues

Do not open a public issue for security problems. Follow [SECURITY.md](SECURITY.md).
