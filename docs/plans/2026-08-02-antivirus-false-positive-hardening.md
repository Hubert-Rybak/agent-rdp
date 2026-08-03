# Antivirus False-Positive Hardening Implementation Plan

> **For Hermes:** Use test-driven development and independent pre-commit review for every behavior change.

**Goal:** Reduce legitimate antivirus/EDR false-positive signals without hiding behavior, bypassing endpoint controls, or weakening the RDP target's security policy.

**Architecture:** Keep the RDP/DVC/Alternate Shell design intact because those behaviors are intrinsic to agent-rdp. Validate the RDP server certificate and respect the target's PowerShell execution policy by default, while retaining explicit, warned compatibility options. Keep release packaging deterministic and document narrowly scoped endpoint-policy guidance without attempting to hide behavior.

**Tech Stack:** Python 3.10+, C++17/Win32, PowerShell 7, GitHub Actions, pytest.

---

### Task 1: Specify safe PowerShell defaults with failing tests

**Objective:** Define that PowerShell command mode does not use `ExecutionPolicy Bypass` unless explicitly requested.

**Files:**
- Create: `tests/test_security_hardening.py`
- Test: `src/launcher/agent_rdp.py`
- Test: `src/windows/agent_rdp_bridge.cpp`

**Steps:**
1. Add parser and FreeRDP command tests expecting certificate validation by default and `/cert:ignore` only for explicit insecure opt-in.
2. Add a parser test expecting `powershell_execution_policy == "default"`.
3. Add an Alternate Shell test expecting no bridge policy argument for the default.
4. Add an explicit compatibility test expecting `--powershell-execution-policy bypass` only when requested.
5. Add a source contract asserting the bridge no longer hardcodes `-ExecutionPolicy Bypass` and uses `-NonInteractive` for one-shot PowerShell.
6. Run `uv run --with pytest python -m pytest tests/test_security_hardening.py -q` and confirm RED for the insecure defaults.

### Task 2: Respect target execution policy by default

**Objective:** Implement the smallest launcher and bridge changes that satisfy Task 1.

**Files:**
- Modify: `src/launcher/agent_rdp.py` (`build_alternate_shell`, `prepare_drive_share`, CLI parser, call chain)
- Modify: `src/windows/agent_rdp_bridge.cpp` (`Args`, argument parsing, PowerShell command construction)

**Steps:**
1. Make certificate validation the default and retain `/cert:ignore` only behind explicit `--insecure-cert-ignore`/legacy `--cert-ignore`, with a warning.
2. Add `--powershell-execution-policy` with choices `default`, `allsigned`, `remotesigned`, `restricted`, `unrestricted`, and `bypass`; default to `default`/no override.
3. Pass a non-default policy to the bridge only for PowerShell mode.
4. Parse and whitelist the policy in the bridge; reject invalid values rather than appending arbitrary command-line text.
5. Construct one-shot PowerShell as `-NoLogo -NoProfile -NonInteractive`, appending `-ExecutionPolicy <policy>` only when explicitly selected.
6. Run the focused test, then `uv run --with pytest python -m pytest tests/ -q` and confirm GREEN.

### Task 3: Document operational hardening without detection evasion

**Objective:** Explain what the changes do and what remains inherently visible to EDR.

**Files:**
- Modify: `README.md`

**Steps:**
1. Document the safe default and the explicit compatibility override.
2. Recommend narrowly scoped policy for an exact reviewed build rather than broad path exclusions.
3. State that remote execution remains visible to endpoint controls.
4. Avoid evasion-oriented recommendations such as obfuscation, encoded commands, security-product exclusions, or disabling Defender.

### Task 4: Verify, review, publish PR, and iterate CI

**Objective:** Deliver a reviewed PR with green Linux and Windows checks.

**Files:** All changed files.

**Steps:**
1. Run `git diff --check`, `python -m py_compile`, focused tests, and the full pytest suite.
2. Run static secret/injection checks on added lines.
3. Dispatch an independent security/code reviewer with the complete diff and fail closed on concerns.
4. Commit as `[verified] security: reduce antivirus false positives`.
5. Push `security/reduce-antivirus-false-positives` and create a PR against `master` with risk/compatibility notes.
6. Monitor both push and pull-request checks, inspect failed logs, and iterate until green.
7. Verify local/tracking/live remote SHAs and key GitHub blob SHAs.
