"""Table Reset flow: drop and recreate every database table after typing RESET."""

from __future__ import annotations

from typing import Any

from core.tui.screens.launch import SAVE_AND_RUN, choose_launch_config, launch_pipeline
from core.tui.screens.repos import ModalTextInputDialog
from core.tui.widgets import ModalDialog

CONFIRM_WORD = "RESET"
RESET_ONLY = "Reset Only"
RESET_AND_PARSE = "Reset & Parse"


def describe_reset_target(cfg: dict[str, Any]) -> str:
    """One-line description of the database a reset with `cfg` drops (engine, schema, address, tunnel)."""
    db = cfg.get("database") if isinstance(cfg.get("database"), dict) else {}
    engine = str(db.get("engine") or "mariadb")
    name = str(db.get("database") or "?")
    addr = f"{db.get('host', '?')}:{db.get('port', '?')}"
    tunnel = db.get("ssh_tunnel") if isinstance(db.get("ssh_tunnel"), dict) else {}
    via = ""
    if tunnel.get("enabled") in (True, "true", "True", 1, "1", "yes", "on"):
        via = f" via SSH {tunnel.get('user') or ''}@{tunnel.get('host') or '?'}".replace(" @", " ")
    return f"{engine} '{name}' at {addr}{via}"


def run_table_reset(stdscr: Any, app: Any, prompt: type[ModalTextInputDialog] = ModalTextInputDialog) -> bool:
    """Confirm and launch a full table reset (`args.reset`, with `args.reset_only` for Reset Only).

    The reset runs with the configuration chosen by `choose_launch_config()` (the saved
    `config.json` unless the user saves first) and only after the user types `RESET` exactly.
    Returns True when the TUI exits into the reset.
    """
    mode = choose_launch_config(stdscr, app, "Reset")
    if mode is None:
        return False
    cfg = app.config if mode == SAVE_AND_RUN else app.disk_config()
    target = describe_reset_target(cfg)

    choice = ModalDialog.show_dialog(
        stdscr,
        title="Table Reset",
        message=(
            "DROPS and recreates EVERY table of\n"
            f"{target}.\n"
            "All parsed data is permanently deleted.\n"
            f"'{RESET_ONLY}' exits after the reset; '{RESET_AND_PARSE}' runs the pipeline next."
        ),
        buttons=[RESET_ONLY, RESET_AND_PARSE, "Cancel"],
        default_button=2,
    )
    if choice not in (RESET_ONLY, RESET_AND_PARSE):
        return False

    typed = prompt(stdscr, title=" Confirm Table Reset ", prompt=f"Type {CONFIRM_WORD} to drop all tables:").show()
    if (typed or "").strip() != CONFIRM_WORD:
        ModalDialog.show_dialog(
            stdscr, "Reset Aborted", f"Confirmation text did not match '{CONFIRM_WORD}'.\nNo tables were changed.", ["OK"]
        )
        return False

    return launch_pipeline(stdscr, app, mode, reset=True, reset_only=(choice == RESET_ONLY))
