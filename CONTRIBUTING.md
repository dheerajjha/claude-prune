# Contributing

Issues and pull requests are welcome. Some things to know first:

- **A real run stops processes.** Any run without `-n` stops old-build Claude Code sessions on
  the machine it runs on. While developing, use `-n`, or set `CLAUDE_PRUNE_HOME` to a temporary
  home with a fake install (see `tests/test_e2e_macos.py` for how to build one).
- **Every rule in the README's "How it decides" came from a real bug.** If you change one, add
  or update the test that covers it in `tests/test_claude_prune.py`.
- **Keep it a single file with no dependencies.** It has to run on the `python3` that ships
  with the Xcode command line tools (3.9).
- **If you port it to shell, watch out for two traps.** In zsh, an unquoted `$VAR` is not
  word-split, so `for p in $LIST` runs once with the whole list. And `awk -v` errors out on a
  multi-line string. Both have bitten earlier versions; that's why it is Python.

Run the tests with:

```sh
python3 -m unittest discover -s tests -v
```

The unit tests are hermetic and run anywhere. The end-to-end test runs only on macOS with
`CLAUDE_PRUNE_E2E=1`, because it starts and stops real (stand-in) processes.
