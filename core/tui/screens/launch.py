"""Shared confirmation flow for TUI actions that leave curses and run the pipeline."""

from __future__ import annotations

from typing import Any

from core.tui.widgets import ModalDialog

SAVE_AND_RUN = "save"
RUN_SAVED = "saved"


def choose_launch_config(stdscr: Any, app: Any, action: str) -> str | None:
    """Ask which configuration a pipeline action runs with.

    Without unsaved edits the saved `config.json` is used directly (`RUN_SAVED`). With unsaved
    edits the user picks `Save & <action>` (`SAVE_AND_RUN`), `<action> with saved config.json`
    (`RUN_SAVED`, edits stay unsaved and unused), or `Cancel` (None).
    """
    if not getattr(app, "is_dirty", False):
        return RUN_SAVED
    save_label = f"Save & {action}"
    saved_label = "Use saved config.json"
    choice = ModalDialog.show_dialog(
        stdscr,
        title="Unsaved Changes",
        message=(
            f"The configuration has unsaved edits.\n"
            f"'{saved_label}' runs with {getattr(app, 'config_path', 'config.json')}\n"
            f"as it is on disk and ignores the edits."
        ),
        buttons=[save_label, saved_label, "Cancel"],
        default_button=2,
    )
    if choice == save_label:
        return SAVE_AND_RUN
    if choice == saved_label:
        return RUN_SAVED
    return None


def launch_pipeline(stdscr: Any, app: Any, mode: str, **launch_args: Any) -> bool:
    """Exit the TUI into the pipeline with `launch_args`; reports a failed save in a dialog."""
    ok, msg = app.request_launch(save=(mode == SAVE_AND_RUN), **launch_args)
    if not ok:
        ModalDialog.show_dialog(stdscr, "Save Failed", msg, ["OK"])
    return ok
