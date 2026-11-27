"""core/tui/screens/api_cfg.py - AI API service configuration screen (`api` section of `config_api.json`)."""

from __future__ import annotations

import curses
import ipaddress
from typing import Any

from core.config import _coerce_bool
from core.tui.screens.base import BaseScreen
from core.tui.screens.parser_dist import wrap_help
from core.tui.screens.repos import ModalTextInputDialog
from core.tui.theme import (
    PAIR_BORDER,
    PAIR_DEFAULT,
    PAIR_HEADER,
    PAIR_MUTED,
    draw_box,
    get_color,
    safe_addstr,
)
from core.tui.widgets import (
    Button,
    Checkbox,
    ModalDialog,
    ScrollableList,
    SelectList,
    StatusBar,
    TextInput,
)

CLIENT_IP_HEADERS: tuple[str, ...] = ("X-Forwarded-For", "X-Real-IP", "CF-Connecting-IP")
SECTIONS: tuple[str, ...] = ("Server", "Keys", "Limits", "OAuth")
_DEFAULT_PROXIES = ["127.0.0.1", "::1"]

HELP_TOPICS: dict[str, tuple[str, list[str]]] = {
    "section": (
        "Settings Section",
        [
            "Switch pages with Left/Right: Server (bind address, proxies), Keys (API keys), Limits (rate limit, auth lockout), OAuth (OAuth 2.1 server).",
            "All pages are saved together.",
        ],
    ),
    "host": (
        "API Bind Host",
        ["Network interface address uvicorn binds to. Defaults to 127.0.0.1 (local only)."],
    ),
    "port": ("API Port", ["TCP port (1-65535) the API service listens on. Defaults to 8001."]),
    "reload": (
        "Auto-Reload",
        ["Enable uvicorn auto-reload on source changes (development only). Disable it for systemd services."],
    ),
    "trusted_proxies": (
        "Trusted Proxies",
        [
            "Comma-separated IP addresses or CIDR networks of reverse proxies whose client-IP header is trusted.",
            "Example: 127.0.0.1, ::1, 10.0.0.0/8",
        ],
    ),
    "client_ip_header": (
        "Client IP Header",
        ["Header carrying the real client address when the request comes from a trusted proxy."],
    ),
    "allow_writes": (
        "Allow Write Operations",
        [
            "Enable write-scoped REST and MCP endpoints (such as saving and mutating NodeMap canvases).",
            "Disabled by default (strictly read-only). Callers also need an API key with 'nodemap:write' or 'write' scope.",
        ],
    ),
    "nodemap_canvas_dir": (
        "Canvas Storage Directory",
        [
            "Persistent directory on disk for saved NodeMap canvas JSON files (shared with the WebApp).",
            "Leave empty to use the default 'nodemap-canvases' directory.",
        ],
    ),
    "keys": (
        "API Keys",
        [
            "Keys are shown masked (first 4 characters of keys of 16+ characters, else only the length) and never in full after entry.",
            "- Add Key: enter a new key (a name is asked for when keys are stored as a name/key map).",
            "- Remove Key: delete the highlighted key.",
            "- Key Scopes: choose scopes ('read', 'nodemap:write', 'write') for the highlighted key.",
        ],
    ),
    "rate_limit": (
        "Rate Limit",
        ["Maximum requests per client within the window in seconds."],
    ),
    "auth_lockout": (
        "Auth Lockout",
        [
            "Clients exceeding Max Failures within the Failure Window are locked out.",
            "The lockout starts at Base Lockout and grows up to Max Lockout (seconds).",
        ],
    ),
    "oauth_enabled": (
        "Enable OAuth 2.1 Server",
        [
            "Enable the built-in MCP OAuth 2.1 authorization server (/.well-known/oauth-* and /api/oauth/*).",
            "Existing X-API-Key and static Bearer API key authentication remain active.",
        ],
    ),
    "oauth_issuer": (
        "OAuth Issuer URL",
        [
            "Public HTTPS base URL of this server (e.g. https://kernelinfo.org).",
            "Leave empty to derive automatically from X-Forwarded-Proto and Host headers.",
        ],
    ),
    "oauth_dcr": (
        "Dynamic Client Registration (RFC 7591)",
        [
            "Allow AI clients to self-register at POST /api/oauth/register with redirect URIs matching allowed_redirect_uris.",
        ],
    ),
    "oauth_cimd": (
        "Client ID Metadata Documents (CIMD)",
        [
            "Allow MCP clients using HTTPS URL client_ids to present metadata documents (fetched with SSRF protection).",
        ],
    ),
    "oauth_data_dir": (
        "OAuth Data Directory",
        [
            "Persistent directory for OAuth signing secret, dynamic clients, auth codes, and refresh tokens.",
            "Leave empty to use the default 'oauth-data' directory.",
        ],
    ),
    "oauth_clients": (
        "Pre-Registered OAuth Clients",
        [
            "Static OAuth 2.1 clients (public PKCE or confidential with a masked client_secret).",
            "- Add Client: configure client_id, name, redirect_uris, and optional client_secret.",
            "- Remove Client: delete the highlighted pre-registered client.",
        ],
    ),
    "overview": (
        "Help",
        [
            "Move to a field to see its help here (F1 opens it in a dialog).",
            "- Server: host, port, reload, trusted proxies, client IP header.",
            "- Keys: API keys, masked.",
            "- Limits: rate limit and auth lockout.",
            "- OAuth: opt-in MCP OAuth 2.1 server and pre-registered clients.",
        ],
    ),
}

_FORM_MIN_W = 52
_HELP_MIN_W = 30


# Shortest key whose first 4 characters may be shown (at most a quarter of it)
MASK_PREFIX_MIN_LEN = 16


def mask_key(key: str) -> str:
    """Masked form of `key`, never the full key: its first 4 characters plus an ellipsis when it has at least
    `MASK_PREFIX_MIN_LEN` characters, otherwise no character of it, only its length."""
    if len(key) >= MASK_PREFIX_MIN_LEN:
        return f"{key[:4]}…"
    return f"•••• ({len(key)} chars)"


SCOPE_PRESETS: tuple[tuple[str, ...], ...] = (
    ("read",),
    ("read", "nodemap:write"),
    ("read", "write"),
)


def _load_keys(raw: Any) -> tuple[list[tuple[str, str]], bool, dict[int, tuple[str, ...]], set[int]]:
    """Return `(keys, stored_as_dict, scopes_by_idx, explicit_scoped_indices)` for `api.keys`."""
    from api.config import VALID_API_SCOPES

    keys: list[tuple[str, str]] = []
    scopes_by_idx: dict[int, tuple[str, ...]] = {}
    explicit_scoped: set[int] = set()
    if isinstance(raw, dict):
        for n, item in raw.items():
            if isinstance(item, str) and item:
                idx = len(keys)
                keys.append((str(n), str(item)))
                scopes_by_idx[idx] = ("read",)
            elif isinstance(item, dict):
                secret = item.get("key") if "key" in item else item.get("secret")
                if isinstance(secret, str) and secret.strip():
                    raw_scopes = item.get("scopes")
                    if isinstance(raw_scopes, (list, tuple, set, frozenset)):
                        valid = tuple(str(s).strip() for s in raw_scopes if isinstance(s, str) and str(s).strip() in VALID_API_SCOPES)
                    else:
                        valid = ("read",)
                    if not valid:
                        valid = ("read",)
                    idx = len(keys)
                    keys.append((str(n), secret.strip()))
                    scopes_by_idx[idx] = valid
                    explicit_scoped.add(idx)
        return keys, True, scopes_by_idx, explicit_scoped
    if isinstance(raw, (list, tuple)):
        for item in raw:
            if isinstance(item, str) and item:
                idx = len(keys)
                keys.append(("", item))
                scopes_by_idx[idx] = ("read",)
        return keys, False, scopes_by_idx, explicit_scoped
    return [], False, {}, set()


def _load_oauth_clients(raw: Any) -> list[dict[str, Any]]:
    """Normalize `api.oauth.clients` into editable dictionaries preserving extra fields."""
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        cid = str(item.get("client_id") or item.get("id") or "").strip()
        if not cid:
            continue
        raw_uris = item.get("redirect_uris")
        if isinstance(raw_uris, str):
            uris = [u.strip() for u in raw_uris.split(",") if u.strip()]
        elif isinstance(raw_uris, (list, tuple)):
            uris = [str(u).strip() for u in raw_uris if isinstance(u, str) and str(u).strip()]
        else:
            uris = []
        if not uris:
            continue
        raw_secret = item.get("client_secret") if "client_secret" in item else item.get("secret")
        secret = str(raw_secret).strip() if isinstance(raw_secret, str) and raw_secret.strip() else ""
        name = str(item.get("name") or item.get("client_name") or cid).strip() or cid
        entry: dict[str, Any] = {
            "client_id": cid,
            "name": name,
            "redirect_uris": uris,
        }
        if secret:
            entry["client_secret"] = secret
        if "max_scopes" in item and isinstance(item["max_scopes"], list):
            entry["max_scopes"] = [str(s).strip() for s in item["max_scopes"] if str(s).strip()]
        out.append(entry)
    return out


class ApiSettingsScreen(BaseScreen):
    """Configuration screen for the standalone AI API service (`api` section)."""

    def __init__(self, stdscr: Any, app: Any = None) -> None:
        super().__init__(stdscr, app)

        if self.app is not None and isinstance(getattr(self.app, "config", None), dict):
            api_cfg = self.app.config.get("api")
            if not isinstance(api_cfg, dict):
                api_cfg = {}
                self.app.config["api"] = api_cfg
        else:
            api_cfg = {}
        self.api_cfg = api_cfg

        rl = api_cfg.get("rate_limit") if isinstance(api_cfg.get("rate_limit"), dict) else {}
        lo = api_cfg.get("auth_lockout") if isinstance(api_cfg.get("auth_lockout"), dict) else {}
        proxies_raw = api_cfg.get("trusted_proxies")
        if isinstance(proxies_raw, (list, tuple)):
            proxies = [str(p) for p in proxies_raw]
        elif isinstance(proxies_raw, str):
            proxies = [p.strip() for p in proxies_raw.split(",") if p.strip()]
        else:
            proxies = list(_DEFAULT_PROXIES)
        hdr = str(api_cfg.get("client_ip_header") or CLIENT_IP_HEADERS[0])
        hdr_idx = CLIENT_IP_HEADERS.index(hdr) if hdr in CLIENT_IP_HEADERS else 0

        def num(src: dict[str, Any], key: str, default: int) -> str:
            return str(src.get(key) if src.get(key) is not None else default)

        self.keys, self.keys_as_dict, self.key_scopes, self.explicit_scoped_keys = _load_keys(api_cfg.get("keys"))
        oauth_raw = api_cfg.get("oauth") if isinstance(api_cfg.get("oauth"), dict) else {}
        self.oauth_clients: list[dict[str, Any]] = _load_oauth_clients(oauth_raw.get("clients"))

        self.sel_section = SelectList(
            y=2, x=4, width=16, label="Section", options=list(SECTIONS), selected_index=0, label_width=20
        )

        # Server page (box y=3)
        self.inp_host = TextInput(
            y=4, x=4, width=26, label="Bind Host", value=str(api_cfg.get("host") or "127.0.0.1"), label_width=20
        )
        self.inp_port = TextInput(
            y=5, x=4, width=12, label="Listen Port", value=num(api_cfg, "port", 8001), label_width=20
        )
        self.chk_reload = Checkbox(
            y=6, x=4, label="Enable Uvicorn Auto-Reload", checked=_coerce_bool(api_cfg.get("reload"), False)
        )
        self.inp_proxies = TextInput(
            y=7, x=4, width=34, label="Trusted Proxies", value=", ".join(proxies), label_width=20, max_len=1024
        )
        self.sel_client_ip_header = SelectList(
            y=8, x=4, width=22, label="Client IP Header", options=list(CLIENT_IP_HEADERS),
            selected_index=hdr_idx, label_width=20,
        )
        self.chk_allow_writes = Checkbox(
            y=9, x=4, label="Allow Write Operations (Nodemap Canvases)",
            checked=_coerce_bool(api_cfg.get("allow_writes"), False),
        )
        self.inp_canvas_dir = TextInput(
            y=10, x=4, width=34, label="Canvas Storage Dir",
            value=str(api_cfg.get("nodemap_canvas_dir") or ""), label_width=20, max_len=1024,
        )

        # Keys page
        self.lst_keys = ScrollableList(
            y=3, x=2, height=9, width=60, items=list(range(len(self.keys))), title="API Keys (masked)",
            render_item_func=self._render_key,
        )
        self.btn_add_key = Button(y=12, x=4, label="Add Key", on_click=self.add_key)
        self.btn_remove_key = Button(y=12, x=18, label="Remove Key", on_click=self.remove_key)
        self.btn_key_scopes = Button(y=12, x=35, label="Key Scopes", on_click=self.edit_key_scopes)

        # Limits page
        self.inp_rl_max = TextInput(
            y=4, x=4, width=12, label="Max Requests", value=num(rl, "max_requests", 120), label_width=22
        )
        self.inp_rl_window = TextInput(
            y=5, x=4, width=12, label="Window (s)", value=num(rl, "window_seconds", 60), label_width=22
        )
        self.inp_lo_max = TextInput(
            y=7, x=4, width=12, label="Max Failures", value=num(lo, "max_failures", 5), label_width=22
        )
        self.inp_lo_window = TextInput(
            y=8, x=4, width=12, label="Failure Window (s)", value=num(lo, "failure_window_seconds", 60), label_width=22
        )
        self.inp_lo_base = TextInput(
            y=9, x=4, width=12, label="Base Lockout (s)", value=num(lo, "base_lockout_seconds", 30), label_width=22
        )
        self.inp_lo_max_lock = TextInput(
            y=10, x=4, width=12, label="Max Lockout (s)", value=num(lo, "max_lockout_seconds", 3600), label_width=22
        )

        # OAuth page
        self.chk_oauth_enabled = Checkbox(
            y=4, x=4, label="Enable OAuth 2.1 Authorization Server",
            checked=_coerce_bool(oauth_raw.get("enabled"), False),
        )
        self.inp_oauth_issuer = TextInput(
            y=5, x=4, width=34, label="Issuer URL",
            value=str(oauth_raw.get("issuer") or ""), label_width=20, max_len=1024,
        )
        self.chk_oauth_dcr = Checkbox(
            y=6, x=4, label="Allow Dynamic Client Registration (DCR)",
            checked=_coerce_bool(oauth_raw.get("allow_dcr"), True),
        )
        self.chk_oauth_cimd = Checkbox(
            y=7, x=4, label="Allow Client ID Metadata Docs (CIMD)",
            checked=_coerce_bool(oauth_raw.get("allow_cimd"), True),
        )
        self.inp_oauth_data_dir = TextInput(
            y=8, x=4, width=34, label="OAuth Data Dir",
            value=str(oauth_raw.get("data_dir") or ""), label_width=20, max_len=1024,
        )
        self.lst_oauth_clients = ScrollableList(
            y=9, x=2, height=4, width=60, items=list(range(len(self.oauth_clients))),
            title="OAuth Clients (secrets masked)",
            render_item_func=self._render_oauth_client,
        )
        self.btn_add_oauth_client = Button(y=13, x=4, label="Add Client", on_click=self.add_oauth_client)
        self.btn_remove_oauth_client = Button(y=13, x=20, label="Remove Client", on_click=self.remove_oauth_client)

        self.btn_save = Button(y=14, x=4, label="Save Settings", on_click=self.save)
        self.btn_cancel = Button(y=14, x=24, label="Cancel", on_click=self.cancel)

        self.status_bar = StatusBar(
            shortcuts=[
                ("Tab/Down", "Next"),
                ("Shift-Tab/Up", "Prev"),
                ("S", "Save"),
                ("F1/?", "Help"),
                ("Esc", "Back"),
            ]
        )

        self._section_widgets: dict[str, list[Any]] = {
            "Server": [
                self.inp_host,
                self.inp_port,
                self.chk_reload,
                self.inp_proxies,
                self.sel_client_ip_header,
                self.chk_allow_writes,
                self.inp_canvas_dir,
            ],
            "Keys": [self.lst_keys, self.btn_add_key, self.btn_remove_key, self.btn_key_scopes],
            "Limits": [
                self.inp_rl_max, self.inp_rl_window, self.inp_lo_max,
                self.inp_lo_window, self.inp_lo_base, self.inp_lo_max_lock,
            ],
            "OAuth": [
                self.chk_oauth_enabled,
                self.inp_oauth_issuer,
                self.chk_oauth_dcr,
                self.chk_oauth_cimd,
                self.inp_oauth_data_dir,
                self.lst_oauth_clients,
                self.btn_add_oauth_client,
                self.btn_remove_oauth_client,
            ],
        }
        self.help_topic_for = {
            id(self.sel_section): "section",
            id(self.inp_host): "host",
            id(self.inp_port): "port",
            id(self.chk_reload): "reload",
            id(self.inp_proxies): "trusted_proxies",
            id(self.sel_client_ip_header): "client_ip_header",
            id(self.chk_allow_writes): "allow_writes",
            id(self.inp_canvas_dir): "nodemap_canvas_dir",
            id(self.lst_keys): "keys",
            id(self.btn_add_key): "keys",
            id(self.btn_remove_key): "keys",
            id(self.btn_key_scopes): "keys",
            id(self.inp_rl_max): "rate_limit",
            id(self.inp_rl_window): "rate_limit",
            id(self.inp_lo_max): "auth_lockout",
            id(self.inp_lo_window): "auth_lockout",
            id(self.inp_lo_base): "auth_lockout",
            id(self.inp_lo_max_lock): "auth_lockout",
            id(self.chk_oauth_enabled): "oauth_enabled",
            id(self.inp_oauth_issuer): "oauth_issuer",
            id(self.chk_oauth_dcr): "oauth_dcr",
            id(self.chk_oauth_cimd): "oauth_cimd",
            id(self.inp_oauth_data_dir): "oauth_data_dir",
            id(self.lst_oauth_clients): "oauth_clients",
            id(self.btn_add_oauth_client): "oauth_clients",
            id(self.btn_remove_oauth_client): "oauth_clients",
        }
        self._rebuild_widgets()
        self.focus_widget(0)
        self.mark_form_clean()

    # --- key list helpers ---

    def _scopes_for(self, idx: int) -> tuple[str, ...]:
        return self.key_scopes.get(idx, ("read",))

    def _render_key(self, idx: int) -> str:
        name, key = self.keys[idx]
        label = name if self.keys_as_dict else f"key {idx + 1}"
        scopes = self._scopes_for(idx)
        if idx in self.explicit_scoped_keys or set(scopes) != {"read"}:
            return f"{label}  {mask_key(key)}  [{', '.join(scopes)}]"
        return f"{label}  {mask_key(key)}"

    def _refresh_key_list(self) -> None:
        self.lst_keys.set_items(list(range(len(self.keys))), keep_selection=True)

    @staticmethod
    def _prompt(stdscr: Any, title: str, prompt: str, default: str = "") -> str | None:
        value = ModalTextInputDialog(stdscr, title=title, prompt=prompt, default_val=default).show()
        return value.strip() if value is not None else None

    def add_key(self) -> bool:
        """Prompt for a new key (and a name for map-form storage); return True when one was added."""
        key = self._prompt(self.stdscr, "Add API Key", "New API key:")
        if not key:
            return False
        if "\x00" in key or any(ch.isspace() for ch in key):
            ModalDialog(self.stdscr, "Invalid Key", "A key cannot contain whitespace or NUL bytes.", ["OK"]).show()
            return False
        if any(key == existing for _n, existing in self.keys):
            ModalDialog(self.stdscr, "Duplicate Key", "That key is already configured.", ["OK"]).show()
            return False
        name = ""
        if self.keys_as_dict:
            used = {n for n, _k in self.keys}
            default = f"key{len(self.keys) + 1}"
            while default in used:
                default += "_"
            name = self._prompt(self.stdscr, "Name API Key", "Key name:", default) or default
            if name in used:
                ModalDialog(self.stdscr, "Duplicate Name", f"A key named '{name}' already exists.", ["OK"]).show()
                return False
        new_idx = len(self.keys)
        self.keys.append((name, key))
        self.key_scopes[new_idx] = ("read",)
        self._refresh_key_list()
        self.lst_keys.select_last()
        return True

    def remove_key(self) -> bool:
        """Remove the highlighted key after confirmation; return True when removed."""
        idx = self.lst_keys.get_selected()
        if idx is None or not 0 <= idx < len(self.keys):
            return False
        choice = ModalDialog.show_dialog(
            self.stdscr,
            title="Remove API Key",
            message=f"Remove key {self._render_key(idx)}?",
            buttons=["Remove", "Cancel"],
            default_button=1,
        )
        if choice != "Remove":
            return False
        del self.keys[idx]
        new_scopes: dict[int, tuple[str, ...]] = {}
        new_explicit: set[int] = set()
        for old_i, sc in self.key_scopes.items():
            if old_i < idx:
                new_scopes[old_i] = sc
                if old_i in self.explicit_scoped_keys:
                    new_explicit.add(old_i)
            elif old_i > idx:
                new_scopes[old_i - 1] = sc
                if old_i in self.explicit_scoped_keys:
                    new_explicit.add(old_i - 1)
        self.key_scopes = new_scopes
        self.explicit_scoped_keys = new_explicit
        self._refresh_key_list()
        return True

    def edit_key_scopes(self) -> bool:
        """Select or edit the scopes for the highlighted API key."""
        from api.config import VALID_API_SCOPES

        idx = self.lst_keys.get_selected()
        if idx is None or not 0 <= idx < len(self.keys):
            return False
        cur_scopes = self._scopes_for(idx)
        raw = self._prompt(
            self.stdscr,
            "API Key Scopes",
            "Comma-separated scopes (read, nodemap:write, write):",
            ", ".join(cur_scopes),
        )
        if raw is None:
            return False
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        invalid = [p for p in parts if p not in VALID_API_SCOPES]
        if not parts or invalid:
            ModalDialog(
                self.stdscr,
                "Invalid Scopes",
                "Allowed scopes are: read, nodemap:write, write.",
                ["OK"],
            ).show()
            return False
        deduped = tuple(dict.fromkeys(parts))
        self.key_scopes[idx] = deduped
        self.explicit_scoped_keys.add(idx)
        if not self.keys_as_dict:
            self.keys_as_dict = True
            self.keys = [(n or f"key{i + 1}", k) for i, (n, k) in enumerate(self.keys)]
        self._refresh_key_list()
        return True

    # --- OAuth client list helpers ---

    def _render_oauth_client(self, idx: int) -> str:
        client = self.oauth_clients[idx]
        cid = str(client.get("client_id") or "")
        name = str(client.get("name") or cid)
        secret = str(client.get("client_secret") or "")
        masked = mask_key(secret) if secret else "(public)"
        uris = client.get("redirect_uris") or []
        label = f"{name} ({cid})" if name and name != cid else cid
        return f"{label}  {masked}  [{', '.join(uris)}]"

    def _refresh_oauth_client_list(self) -> None:
        self.lst_oauth_clients.set_items(list(range(len(self.oauth_clients))), keep_selection=True)

    def add_oauth_client(self) -> bool:
        """Prompt for a pre-registered OAuth client; return True when added."""
        cid = self._prompt(self.stdscr, "Add OAuth Client", "Client ID:")
        if not cid:
            return False
        if "\x00" in cid or any(ch.isspace() for ch in cid):
            ModalDialog(self.stdscr, "Invalid Client ID", "Client ID cannot contain whitespace or NUL bytes.", ["OK"]).show()
            return False
        if any(c.get("client_id") == cid for c in self.oauth_clients):
            ModalDialog(self.stdscr, "Duplicate Client ID", f"An OAuth client '{cid}' already exists.", ["OK"]).show()
            return False
        name = self._prompt(self.stdscr, "OAuth Client Name", "Display name:", cid) or cid
        raw_uris = self._prompt(
            self.stdscr,
            "OAuth Redirect URIs",
            "Comma-separated redirect URIs (https://... or loopback http://...):",
        )
        if not raw_uris:
            return False
        uris = [u.strip() for u in raw_uris.split(",") if u.strip()]
        if not uris or any("\x00" in u or any(ch.isspace() for ch in u) for u in uris):
            ModalDialog(self.stdscr, "Invalid Redirect URIs", "Provide at least one valid redirect URI.", ["OK"]).show()
            return False
        for u in uris:
            lower = u.lower()
            if lower.startswith(("javascript:", "data:", "vbscript:")):
                ModalDialog(self.stdscr, "Invalid Redirect URI", f"Forbidden URI scheme in '{u}'.", ["OK"]).show()
                return False
            if lower.startswith("http://") and not lower.startswith(("http://localhost", "http://127.0.0.1", "http://[::1]")):
                ModalDialog(
                    self.stdscr,
                    "Invalid Redirect URI",
                    "Plain http:// is only allowed for loopback hosts (localhost, 127.0.0.1, [::1]).",
                    ["OK"],
                ).show()
                return False
        secret = self._prompt(
            self.stdscr,
            "OAuth Client Secret",
            "Client secret (leave empty for public PKCE client):",
            "",
        )
        if secret is None:
            secret = ""
        if secret and ("\x00" in secret or any(ch.isspace() for ch in secret)):
            ModalDialog(self.stdscr, "Invalid Secret", "Client secret cannot contain whitespace or NUL bytes.", ["OK"]).show()
            return False
        entry: dict[str, Any] = {
            "client_id": cid,
            "name": name,
            "redirect_uris": uris,
        }
        if secret:
            entry["client_secret"] = secret
        self.oauth_clients.append(entry)
        self._refresh_oauth_client_list()
        self.lst_oauth_clients.select_last()
        return True

    def remove_oauth_client(self) -> bool:
        """Remove the highlighted pre-registered OAuth client after confirmation."""
        idx = self.lst_oauth_clients.get_selected()
        if idx is None or not 0 <= idx < len(self.oauth_clients):
            return False
        choice = ModalDialog.show_dialog(
            self.stdscr,
            title="Remove OAuth Client",
            message=f"Remove OAuth client {self._render_oauth_client(idx)}?",
            buttons=["Remove", "Cancel"],
            default_button=1,
        )
        if choice != "Remove":
            return False
        del self.oauth_clients[idx]
        self._refresh_oauth_client_list()
        return True

    # --- widgets / form state ---

    def active_section(self) -> str:
        return self.sel_section.get_selected()

    def _rebuild_widgets(self) -> None:
        self.widgets = [self.sel_section, *self._section_widgets[self.active_section()], self.btn_save, self.btn_cancel]

    def form_snapshot(self) -> list[Any]:
        values: list[Any] = []
        for widgets in self._section_widgets.values():
            for w in widgets:
                if hasattr(w, "get_value"):
                    values.append(w.get_value())
                elif hasattr(w, "is_checked"):
                    values.append(w.is_checked())
                elif hasattr(w, "options"):
                    values.append(w.get_selected())
        values.append(list(self.keys))
        values.append(dict(self.key_scopes))
        values.append([dict(c) for c in self.oauth_clients])
        return values

    def current_help_topic(self) -> str:
        return self.help_topic_for.get(id(self.get_focused_widget()), "overview")

    def show_help_dialog(self) -> None:
        title, paragraphs = HELP_TOPICS[self.current_help_topic()]
        ModalDialog(self.stdscr, title, "\n".join(paragraphs), ["OK"]).show()

    def _layout_widths(self, max_x: int) -> tuple[int, int]:
        if max_x - 4 - 1 - _FORM_MIN_W < _HELP_MIN_W:
            return max(40, min(76, max_x - 4)), 0
        box_w = min(76, max(_FORM_MIN_W, max_x - 4 - 1 - 40))
        return box_w, max_x - 2 - box_w - 1 - 2

    def _draw_help_panel(self, y: int, x: int, h: int, w: int) -> None:
        title, paragraphs = HELP_TOPICS[self.current_help_topic()]
        draw_box(self.stdscr, y, x, h, w, title=title, color_pair=PAIR_BORDER)
        inner_h = h - 2
        lines = wrap_help(paragraphs, w - 4)
        if len(lines) > inner_h:
            lines = lines[: max(0, inner_h - 1)] + ["... F1: full text"]
        for i, line in enumerate(lines):
            attr = get_color(PAIR_MUTED) if line.startswith("... F1") else get_color(PAIR_DEFAULT)
            safe_addstr(self.stdscr, y + 1 + i, x + 2, line, attr)

    def _validate_fields(self) -> tuple[dict[str, Any], list[str]]:
        """Validate all inputs; return (parsed values, errors)."""
        values: dict[str, Any] = {}
        errors: list[str] = []

        host = self.inp_host.get_value().strip()
        if not host or "\x00" in host:
            errors.append("Bind Host: must be a non-empty string without NUL bytes")
        else:
            values["host"] = host

        int_specs = (
            ("port", self.inp_port, 65535),
            ("max_requests", self.inp_rl_max, None),
            ("window_seconds", self.inp_rl_window, None),
            ("max_failures", self.inp_lo_max, None),
            ("failure_window_seconds", self.inp_lo_window, None),
            ("base_lockout_seconds", self.inp_lo_base, None),
            ("max_lockout_seconds", self.inp_lo_max_lock, None),
        )
        for key, widget, max_val in int_specs:
            raw = widget.get_value().strip()
            try:
                num = int(raw)
            except (ValueError, TypeError):
                num = None
            if num is None or num < 1 or (max_val is not None and num > max_val):
                desc = "an integer between 1 and 65535" if max_val else "a positive whole number"
                errors.append(f"{widget.label}: '{raw}' must be {desc}")
            else:
                values[key] = num
        if (
            "base_lockout_seconds" in values
            and "max_lockout_seconds" in values
            and values["max_lockout_seconds"] < values["base_lockout_seconds"]
        ):
            errors.append("Max Lockout (s): must be >= Base Lockout (s)")

        proxies = [p.strip() for p in self.inp_proxies.get_value().split(",") if p.strip()]
        for p in proxies:
            try:
                ipaddress.ip_network(p, strict=False)
            except ValueError:
                errors.append(f"Trusted Proxies: '{p}' is not an IP address or CIDR network")
        values["trusted_proxies"] = proxies

        canvas_dir = self.inp_canvas_dir.get_value().strip()
        if "\x00" in canvas_dir:
            errors.append("Canvas Storage Dir: cannot contain NUL bytes")
        else:
            values["nodemap_canvas_dir"] = canvas_dir

        oauth_issuer = self.inp_oauth_issuer.get_value().strip().rstrip("/")
        if "\x00" in oauth_issuer or any(ch.isspace() for ch in oauth_issuer):
            errors.append("Issuer URL: cannot contain whitespace or NUL bytes")
        elif oauth_issuer and not (
            oauth_issuer.startswith("https://")
            or oauth_issuer.startswith(("http://localhost", "http://127.0.0.1", "http://[::1]"))
        ):
            errors.append("Issuer URL: must use https:// (or loopback http:// for local testing)")
        else:
            values["oauth_issuer"] = oauth_issuer

        oauth_data_dir = self.inp_oauth_data_dir.get_value().strip()
        if "\x00" in oauth_data_dir:
            errors.append("OAuth Data Dir: cannot contain NUL bytes")
        else:
            values["oauth_data_dir"] = oauth_data_dir
        return values, errors

    def draw(self) -> None:
        """Render the API settings screen."""
        self.clear()
        max_y, max_x = self.get_maxyx()
        safe_addstr(self.stdscr, 1, 2, "API Service Configuration", get_color(PAIR_HEADER) | curses.A_BOLD)
        box_w, help_w = self._layout_widths(max_x)
        section = self.active_section()
        if section == "Server":
            draw_box(self.stdscr, 3, 2, 9, box_w, title="API Server", color_pair=PAIR_BORDER)
        elif section == "Limits":
            draw_box(self.stdscr, 3, 2, 3, box_w, title="Rate Limit", color_pair=PAIR_BORDER)
            draw_box(self.stdscr, 6, 2, 6, box_w, title="Auth Lockout", color_pair=PAIR_BORDER)
        elif section == "OAuth":
            draw_box(self.stdscr, 3, 2, 6, box_w, title="OAuth 2.1 Server", color_pair=PAIR_BORDER)
            self.lst_oauth_clients.width = max(20, box_w)
        else:
            self.lst_keys.width = max(20, box_w)
        if help_w:
            self._draw_help_panel(3, 2 + box_w + 1, max(4, max_y - 4), help_w)
        for widget in self.widgets:
            if hasattr(widget, "draw"):
                widget.draw(self.stdscr)
        self.status_bar.draw(self.stdscr)
        self.refresh()

    def handle_key(self, ch: int) -> bool:
        """Handle navigation, shortcuts, and widget key events."""
        if ch in (9, curses.KEY_DOWN):
            focused_list = self.get_focused_widget()
            if (
                ch == curses.KEY_DOWN
                and focused_list in (self.lst_keys, self.lst_oauth_clients)
                and focused_list.selected_index < len(focused_list.items) - 1
            ):
                focused_list.select_next()
            else:
                self.focus_next()
            return True
        if ch in (curses.KEY_UP, getattr(curses, "KEY_BTAB", 353), 353):
            focused_list = self.get_focused_widget()
            if focused_list in (self.lst_keys, self.lst_oauth_clients) and focused_list.selected_index > 0:
                focused_list.select_prev()
            else:
                self.focus_prev()
            return True
        if ch == 27:
            self.cancel()
            return True
        if ch == 19:
            self.save()
            return True
        if ch == getattr(curses, "KEY_F1", 265):
            self.show_help_dialog()
            return True

        focused = self.get_focused_widget()
        if focused is self.lst_keys and ch in (curses.KEY_DC, ord("d"), ord("D")):
            self.remove_key()
            return True
        if focused is self.lst_oauth_clients and ch in (curses.KEY_DC, ord("d"), ord("D")):
            self.remove_oauth_client()
            return True
        if focused is not None and hasattr(focused, "handle_key") and focused.handle_key(ch):
            if focused is self.sel_section:
                self._rebuild_widgets()
                self.focus_widget(0)
            return True
        if ch == ord("?"):
            self.show_help_dialog()
            return True
        if ch in (ord("s"), ord("S")):
            self.save()
            return True
        return False

    def _keys_value(self) -> Any:
        has_scoped = any(idx in self.explicit_scoped_keys or set(self._scopes_for(idx)) != {"read"} for idx in range(len(self.keys)))
        if self.keys_as_dict or has_scoped:
            out: dict[str, Any] = {}
            for idx, (name, key) in enumerate(self.keys):
                label = name or f"key{idx + 1}"
                scopes = self._scopes_for(idx)
                if idx in self.explicit_scoped_keys or set(scopes) != {"read"}:
                    out[label] = {"key": key, "scopes": list(scopes)}
                else:
                    out[label] = key
            return out
        return [key for _name, key in self.keys]

    def save(self) -> bool:
        """Validate and persist settings to `app.config['api']`, mark dirty, and pop the screen."""
        parsed, errors = self._validate_fields()
        if errors:
            ModalDialog(
                self.stdscr, "Invalid Value", "Settings were not saved:\n" + "\n".join(errors), ["OK"]
            ).show()
            return False

        if self.app is not None and isinstance(getattr(self.app, "config", None), dict):
            api_cfg = self.app.config.setdefault("api", {})
            if not isinstance(api_cfg, dict):
                api_cfg = {}
                self.app.config["api"] = api_cfg
        else:
            api_cfg = self.api_cfg

        api_cfg["host"] = parsed["host"]
        api_cfg["port"] = parsed["port"]
        api_cfg["reload"] = self.chk_reload.is_checked()
        api_cfg["allow_writes"] = self.chk_allow_writes.is_checked()
        api_cfg["nodemap_canvas_dir"] = parsed["nodemap_canvas_dir"]
        api_cfg["trusted_proxies"] = parsed["trusted_proxies"]
        api_cfg["client_ip_header"] = self.sel_client_ip_header.get_selected()
        api_cfg["keys"] = self._keys_value()
        api_cfg["rate_limit"] = {
            "max_requests": parsed["max_requests"],
            "window_seconds": parsed["window_seconds"],
        }
        api_cfg["auth_lockout"] = {
            "max_failures": parsed["max_failures"],
            "failure_window_seconds": parsed["failure_window_seconds"],
            "base_lockout_seconds": parsed["base_lockout_seconds"],
            "max_lockout_seconds": parsed["max_lockout_seconds"],
        }
        existing_oauth = api_cfg.get("oauth") if isinstance(api_cfg.get("oauth"), dict) else {}
        oauth_out = dict(existing_oauth)
        oauth_out["enabled"] = self.chk_oauth_enabled.is_checked()
        oauth_out["issuer"] = parsed["oauth_issuer"]
        oauth_out["allow_dcr"] = self.chk_oauth_dcr.is_checked()
        oauth_out["allow_cimd"] = self.chk_oauth_cimd.is_checked()
        oauth_out["data_dir"] = parsed["oauth_data_dir"]
        oauth_out["clients"] = [dict(c) for c in self.oauth_clients]
        api_cfg["oauth"] = oauth_out
        self.api_cfg = api_cfg

        if self.app is not None:
            if hasattr(self.app, "is_dirty"):
                self.app.is_dirty = True
            if hasattr(self.app, "pop_screen") and callable(self.app.pop_screen):
                self.app.pop_screen()
        self.mark_form_clean()
        return True

    def cancel(self) -> None:
        """Dismiss the screen without persisting (confirming first when edits are unsaved)."""
        if not self.confirm_discard():
            return
        if self.app is not None and hasattr(self.app, "pop_screen") and callable(self.app.pop_screen):
            self.app.pop_screen()
