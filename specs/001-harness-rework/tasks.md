# Tasks

P0
- [x] Audit and threat model (audit.md)
- [x] Spec, plan, tasks
- [ ] Commit, handoff

P1 (lane only, no deploy)
- [ ] T1 `capabilities.py`: load, resolve, changed, receipt, rate count, disabled. Self-tests in the module (`python3 capabilities.py --self-test`).
- [ ] T2 poller: `capability:` body path beside `argv:`; kill switch; rate cap; class gate with comment approval; actor lookup; receipt and comment; F8 fix. Extend `--self-test`.
- [ ] T3 `harness` helper with enumerated actions; `bash -n` and shellcheck clean; a dry-run self-check.
- [ ] T4 README capabilities section and secrets table; AGENTS.md body form and approval flow; commit TRUST-BOUNDARY.md.
- [ ] T5 `/ponytail-review`, commit, handoff.

P2 (owner)
- [ ] Config migration, kickstart, sentinel job, receipt check.

P3 (owner decisions 1 to 7 in plan.md)
