"""
verifier.py - Checks whether a proposed patch actually counts as a fix.

The spec's bar: the repro passes with the patch applied in the sandbox AND the
existing tests still pass. Building Mattermost from source with a patch is slow,
so the verifier is a slot with three fillings:

  * NoVerifier      (default) Can't check anything, so no fix ever counts and the
                    case is handed off with the patch listed as "not verified".
  * CommandVerifier Runs a command YOU provide (TROUBLESHOOT_VERIFY_CMD). It gets the
                    patch file and the repro script as arguments and must print JSON:
                    {"repro_passed": true, "tests_passed": false,
                     "broken_tests": ["TestX"], "notes": "..."}
                    That command is where the "apply patch, build image, run repro,
                    run tests" work lives, so it can change without touching this code.
  * RecordedVerifier Fixed answers, for tests and scripted demos.
"""
import json
import shlex
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass
class VerifyResult:
    verified: bool
    repro_passed: bool | None = None
    tests_passed: bool | None = None
    broken_tests: list[str] = field(default_factory=list)
    notes: str = ""


class PatchVerifier(Protocol):
    can_verify: bool

    def verify(self, patch: str, target_cause: str | None) -> VerifyResult: ...


class NoVerifier:
    can_verify = False

    def verify(self, patch: str, target_cause: str | None) -> VerifyResult:
        return VerifyResult(verified=False, notes="No patch verifier configured (set TROUBLESHOOT_VERIFY_CMD).")


class CommandVerifier:
    can_verify = True

    def __init__(self, command: str, repro_script: str | None, timeout_s: int = 3600):
        self.argv = shlex.split(command)       # never run through a shell
        self.repro_script = repro_script
        self.timeout_s = timeout_s

    def verify(self, patch: str, target_cause: str | None) -> VerifyResult:
        if not self.repro_script:
            return VerifyResult(verified=False, notes="No repro script, so there is nothing to verify against.")
        with tempfile.TemporaryDirectory() as tmp:
            patch_file, repro_file = Path(tmp, "fix.patch"), Path(tmp, "repro.py")
            patch_file.write_text(patch)
            repro_file.write_text(self.repro_script)
            try:
                done = subprocess.run([*self.argv, str(patch_file), str(repro_file)],
                                      capture_output=True, text=True, timeout=self.timeout_s)
            except subprocess.TimeoutExpired:
                return VerifyResult(verified=False, notes=f"Verifier timed out after {self.timeout_s}s.")
        try:
            data = json.loads(done.stdout.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError):
            return VerifyResult(verified=False,
                                notes=f"Verifier gave no JSON (exit {done.returncode}): {done.stderr[-500:]}")
        return VerifyResult(verified=True, repro_passed=bool(data.get("repro_passed")),
                            tests_passed=bool(data.get("tests_passed")),
                            broken_tests=[str(t) for t in data.get("broken_tests", [])],
                            notes=str(data.get("notes", ""))[:1000])


class RecordedVerifier:
    """Answers by target cause id (or '*' for any). Unknown causes: the repro still fails."""
    can_verify = True

    def __init__(self, outcomes: dict[str, VerifyResult]):
        self.outcomes = outcomes
        self.calls: list[str | None] = []

    def verify(self, patch: str, target_cause: str | None) -> VerifyResult:
        self.calls.append(target_cause)
        return self.outcomes.get(target_cause or "", self.outcomes.get(
            "*", VerifyResult(verified=True, repro_passed=False, tests_passed=None, notes="recorded: repro still fails")))
