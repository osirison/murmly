"""Removing Murmly from the app: plain Python, no Qt.

The window asks for a plan, shows what removal would take away, and removes only
after the person confirms. What is shown comes from the installer's own list of
what it installed (`removal.plan_removal`), not from text kept here, so the list
cannot name something `murmly uninstall` does not remove or miss something it
does. The removal itself is `removal.remove_murmly`, the function the command
runs.

Deleting settings and downloaded models is one separate switch that starts off
and is only ever turned on by the person. Nothing here turns it on.

The Qt side (`controller.RemovalController`) runs the plan and the removal on the
worker thread and forwards the person's clicks; every rule is here.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from murmly.installer import Installer
from murmly.removal import RemovalPlan, RemovalResult, plan_removal, remove_murmly

IDLE = "idle"
PLANNING = "planning"
CONFIRMING = "confirming"
REMOVING = "removing"
DONE = "done"
FAILED = "failed"

TITLE_CONFIRM = "Remove murmly from this computer?"
TITLE_REMOVING = "Removing murmly…"
TITLE_DONE = "Murmly was removed"
TITLE_FAILED = "Murmly could not be removed"

INTRO_ITEMS = "This stops murmly and removes the following from this computer."
INTRO_NOTHING = "Murmly does not look installed on this computer, so there is nothing to stop or remove."
KEPT_NOTE = "Your settings and downloaded models are kept."
PURGE_TITLE = "Also delete my settings and downloaded models"
PURGE_NOTE = "These are deleted as well, and this cannot be undone."
#: The transcription model is downloaded by Hugging Face's own cache, shared with other
#: programs and outside the folders above, so a purge never touches it.
PURGE_FOOTNOTE = "The transcription model lives in the shared Hugging Face cache and is not deleted."
CANCEL_LABEL = "Keep murmly"
CONFIRM_LABEL = "Remove murmly"
CONFIRM_PURGE_LABEL = "Remove murmly and delete my data"
DONE_NOTE = "The app was using what was just removed, so it has to close."
DONE_LABEL = "Close murmly"
FAILED_LABEL = "Close"

Plan = Callable[[], RemovalPlan]
Remove = Callable[[bool], RemovalResult]


def default_plan() -> RemovalPlan:
    """What removal would take away on this computer, read now."""
    return plan_removal(Installer(announce=lambda _line: None))


def default_remove(purge: bool) -> RemovalResult:
    """`murmly uninstall`, with `--purge`'s deletion when `purge` is true. Blocking."""
    return remove_murmly(Installer(announce=lambda _line: None), purge_user_files=purge)


@dataclass(slots=True)
class RemovalState:
    """Where a removal is. Every change goes through a method that checks the step.

    `confirm_ready` is what stops a click that arrives when no plan was shown: only
    a plan on screen (`CONFIRMING`) can be confirmed, and only once.
    """

    step: str = IDLE
    plan: RemovalPlan | None = None
    purge: bool = False
    report: tuple[str, ...] = ()
    error: str = ""

    def request(self) -> bool:
        """The person asked to remove murmly. True when a plan should be read."""
        if self.step not in (IDLE, FAILED, DONE):
            return False
        self.step = PLANNING
        self.plan = None
        self.purge = False
        self.report = ()
        self.error = ""
        return True

    def planned(self, plan: RemovalPlan | None, error: str = "") -> None:
        if self.step != PLANNING:
            return
        if plan is None:
            self.step = FAILED
            self.error = error or "Murmly could not read what is installed."
            return
        self.plan = plan
        self.step = CONFIRMING

    def set_purge(self, purge: bool) -> None:
        """The person's choice, and only theirs. Ignored when no plan is on screen."""
        if self.step == CONFIRMING:
            self.purge = bool(purge)

    def cancel(self) -> None:
        if self.step in (PLANNING, CONFIRMING, FAILED):
            self.step = IDLE
            self.plan = None
            self.purge = False
            self.error = ""

    def confirm(self) -> bool:
        """The person confirmed. True exactly once per plan shown: removal should run."""
        if self.step != CONFIRMING:
            return False
        self.step = REMOVING
        return True

    def removed(self, result: RemovalResult) -> None:
        if self.step != REMOVING:
            return
        self.report = tuple(result.messages)
        self.step = DONE

    def failed(self, error: str) -> None:
        if self.step != REMOVING:
            return
        self.error = error or "Removal stopped without saying why."
        self.step = FAILED

    @property
    def finished(self) -> bool:
        return self.step == DONE


def confirm_lines(plan: RemovalPlan) -> list[dict[str, str]]:
    """One line per installed thing, from the installer's own list."""
    return [{"label": item.label, "detail": item.detail} for item in plan.items]


def removal_view(state: RemovalState) -> dict:
    """Everything the dialog shows, as strings and flags."""
    open_ = state.step in (CONFIRMING, REMOVING, DONE, FAILED)
    view: dict = {
        "step": state.step,
        "open": open_,
        "busy": state.step in (PLANNING, REMOVING),
        "title": "",
        "intro": "",
        "items": [],
        "purge": state.purge,
        "purgeTitle": PURGE_TITLE,
        "purgeNote": "",
        "purgeFootnote": "",
        "purgeTargets": [],
        "keptNote": "",
        "cancelLabel": CANCEL_LABEL,
        "confirmLabel": CONFIRM_LABEL,
        "report": [],
        "doneNote": "",
        "doneLabel": DONE_LABEL,
        "error": "",
        "failedLabel": FAILED_LABEL,
    }
    if state.step == CONFIRMING and state.plan is not None:
        plan = state.plan
        view["title"] = TITLE_CONFIRM
        view["intro"] = INTRO_ITEMS if plan.items else INTRO_NOTHING
        view["items"] = confirm_lines(plan)
        view["purgeTargets"] = [
            {"label": target.label, "path": str(target.path)} for target in plan.purge_targets
        ]
        view["purgeNote"] = PURGE_NOTE if state.purge else ""
        view["purgeFootnote"] = PURGE_FOOTNOTE
        view["keptNote"] = "" if state.purge else KEPT_NOTE
        view["confirmLabel"] = CONFIRM_PURGE_LABEL if state.purge else CONFIRM_LABEL
    elif state.step == REMOVING:
        view["title"] = TITLE_REMOVING
    elif state.step == DONE:
        view["title"] = TITLE_DONE
        view["report"] = list(state.report)
        view["doneNote"] = DONE_NOTE
    elif state.step == FAILED:
        view["title"] = TITLE_FAILED
        view["error"] = state.error
    return view


class RemovalModel:
    """What the controller runs on the worker: the plan, and the removal.

    Both are injected, so a test never reads or removes what is installed on the
    machine it runs on.
    """

    def __init__(self, plan: Plan = default_plan, remove: Remove = default_remove) -> None:
        self.plan = plan
        self.remove = remove
