"""Builder control policy: the one piece of builder code the self-improvement loop may rewrite.

Evolved versions live in data/ and are loaded through paperci/policy_sandbox.py, which enforces this contract
before any version runs:
- Pure functions of `state`; no imports (the `re` module is provided), no file, network or process access,
  no names starting with "_", no while loops, no exponentiation, only the state keys listed below.
- `state` never holds sealed claim values, the paper's numbers, tolerances or anything from the verifier,
  so a policy cannot leak or target the answers even if it tried.

state keys: turn, max_turns, run_by_turn, script_by_turn, single_target, controller (bool: the learned
controller is on), submit_attempts, fired (list of milestone keys already used), actions (list of dicts:
type "shell" with cmd and exit, or type "write" with path), failures (list of failure-class names so far).
"""

POLICY_NAME = "baseline"

READ_ONLY = r"\s*(ls|cat|head|tail|grep|find|sed -n|pip3? |wc|echo|#)"


def ran_repo_code(state):
    for a in state["actions"]:
        if a["type"] == "shell" and not re.match(READ_ONLY, a["cmd"]):
            return True
    return False


def wrote_script(state):
    for a in state["actions"]:
        if a["type"] == "write" and a["path"] == "/work/paperci_run.sh":
            return True
        if a["type"] == "shell" and "paperci_run.sh" in a["cmd"] and ">" in a["cmd"]:
            return True
    return False


def system_addendum(state):
    """Extra system-prompt text for this build (state has no actions yet), or ""."""
    return ""


def milestone(state):
    """A mandatory instruction to inject this turn, as {"key": ..., "message": ...}, or None."""
    if not state["controller"]:
        return None
    if state["turn"] == state["run_by_turn"] and "run" not in state["fired"] and not ran_repo_code(state):
        return {"key": "run", "message": (
            "CONTROLLER (mandatory): you have not executed any repository code yet. Stop reading. In THIS turn, "
            "run the smallest end-to-end command that exercises the repo's code path for the easiest target "
            "(e.g. a tiny config / few iterations), or call give_up if the target is impossible here.")}
    if state["turn"] == state["script_by_turn"] and "script" not in state["fired"] and not wrote_script(state):
        return {"key": "script", "message": (
            "CONTROLLER (mandatory): write /work/paperci_run.sh NOW for the single target you can already compute "
            "(reduced scale is fine, declare it), test it once, then call submit. Do not explore further.")}
    return None


def late_nudge(state):
    """A reminder when the turn budget is nearly spent, or None."""
    if state["max_turns"] - state["turn"] == 2:
        return "Only 2 turns left: write /work/paperci_run.sh now and submit, or give_up."
    return None
