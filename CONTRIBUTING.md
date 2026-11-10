# Contributing

Thanks for helping. A few rules keep vnodes small and trustworthy.

- **Standard library only.** No third-party packages at runtime or in tests. Python 3.9+, `git`.
- **Tests first, all green.** `python3 -m unittest` must pass. New behaviour comes with a test; a bug fix comes
  with the test that would have caught it.
- **Nothing bypasses plan -> approve -> apply.** No code path may write `nodes.json` except `apply` and `init`.
  Discovery sources only propose (see `docs/DISCOVERY.md`).
- **Identity comes from the OS.** Never add a flag, env var or config key that lets a user say who they are.
- **Schema changes are additive within a major.** A new optional field is a minor version (`x-schema-version`);
  anything that makes a valid file invalid is a new document `version` and needs a migration plan.
- **No real data, ever.** Examples and tests use RFC 5737 addresses (`192.0.2.0/24`, `198.51.100.0/24`,
  `203.0.113.0/24`), RFC 7042 MAC addresses (`02:00:5e:00:53:xx`) and `.example` names. Before you open a pull
  request, run the leak scan against your own machine and, if you ever pointed your checkout at a real
  inventory, against those files:

  ```sh
  VNODES_LEAK_STRICT=1 VNODES_LEAK_SOURCES=/path/to/your/nodes.json python3 -m unittest tests.test_leak_scan
  ```

- Keep commits focused, with a message that says why. By contributing you agree that your contribution is licensed
  under the Apache License 2.0.
