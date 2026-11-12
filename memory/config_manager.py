import json
import sys
from pathlib import Path

def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent

BASE_DIR    = get_base_dir()
CONFIG_DIR  = BASE_DIR / "config"
CONFIG_FILE = CONFIG_DIR / "api_keys.json"
_KEYRING_SERVICE = "TradBot-I"


def _keyring():
    try:
        import keyring
    except ImportError as exc:
        raise RuntimeError(
            "The OS credential vault is unavailable. Install the declared "
            "'keyring' dependency and retry setup."
        ) from exc
    try:
        backend = keyring.get_keyring()
    except keyring.errors.KeyringError as exc:
        raise RuntimeError("The operating system's credential vault could not be opened.") from exc
    if getattr(backend, "priority", 0) <= 0:
        raise RuntimeError(
            "No usable OS credential vault is available. Configure the "
            "Windows Credential Manager, macOS Keychain, or a Linux Secret Service."
        )
    return keyring

def ensure_config_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

def config_exists() -> bool:
    return CONFIG_FILE.exists()

def save_api_keys(gemini_api_key: str) -> None:
    data = load_api_keys()
    vault = _keyring()
    try:
        vault.set_password(
            _KEYRING_SERVICE, "gemini_api_key", gemini_api_key.strip()
        )
    except vault.errors.KeyringError as exc:
        raise RuntimeError("The OS credential vault could not save the Gemini key.") from exc
    data.pop("gemini_api_key", None)
    data["gemini_key_in_keyring"] = True
    ensure_config_dir()
    CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")

def load_api_keys() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"❌ Failed to load api_keys.json: {e}")
        return {}

def get_gemini_key() -> str | None:
    config = load_api_keys()
    if config.get("gemini_key_in_keyring"):
        return _keyring().get_password(_KEYRING_SERVICE, "gemini_api_key")
    return config.get("gemini_api_key")


def require_gemini_key() -> str:
    key = get_gemini_key()
    if not key:
        raise RuntimeError("Gemini API key is not configured. Complete setup first.")
    return key


def get_mt5_credentials() -> dict | None:
    return get_mt5_credentials_for_account()


def get_mt5_accounts() -> list[dict]:
    """Return configured read-only MT5 account metadata, without credentials."""
    config = load_api_keys()
    accounts = config.get("mt5_accounts")
    if isinstance(accounts, list):
        return [
            dict(account)
            for account in accounts
            if isinstance(account, dict)
            and str(account.get("login", "")).strip()
            and str(account.get("server", "")).strip()
        ]
    login = str(config.get("mt5_login", "")).strip()
    server = str(config.get("mt5_server", "")).strip()
    if not login or not server:
        return []
    return [{
        "id": f"{login}@{server}",
        "name": f"{login}@{server}",
        "login": login,
        "server": server,
    }]


def get_mt5_credentials_for_account(account_id: str = "") -> dict | None:
    config = load_api_keys()
    accounts = get_mt5_accounts()
    if not accounts:
        return None
    selected_id = str(account_id or "").strip()
    if not selected_id:
        selected_id = str(config.get("mt5_active_account_id", "")).strip()
    account = next(
        (
            row for row in accounts
            if str(row.get("id") or f"{row['login']}@{row['server']}")
            == selected_id
        ),
        None,
    ) if selected_id else None
    if selected_id and account is None:
        raise ValueError(f"Unknown MT5 account identifier: {selected_id}")
    if account is None:
        account = accounts[0]
    login = str(account["login"]).strip()
    server = str(account["server"]).strip()
    key = f"mt5:{login}:{server}"
    password = _keyring().get_password(_KEYRING_SERVICE, key)
    if not password:
        raise RuntimeError(
            "The MT5 investor password is missing from the OS credential vault. "
            "Re-enter this account's investor credentials in trading settings."
        )
    return {"login": int(login), "server": server, "password": password}


def get_mt5_account_id(account_id: str = "") -> str:
    accounts = get_mt5_accounts()
    if not accounts:
        return ""
    requested = str(account_id or "").strip()
    if requested:
        for account in accounts:
            identifier = str(
                account.get("id") or f"{account['login']}@{account['server']}"
            )
            if identifier == requested:
                return identifier
        raise ValueError(f"Unknown MT5 account identifier: {requested}")
    config = load_api_keys()
    selected = str(config.get("mt5_active_account_id", "")).strip()
    for account in accounts:
        identifier = str(account.get("id") or f"{account['login']}@{account['server']}")
        if identifier == selected:
            return identifier
    account = accounts[0]
    return str(account.get("id") or f"{account['login']}@{account['server']}")


def get_mt5_account_ids() -> list[str]:
    return [
        str(account.get("id") or f"{account['login']}@{account['server']}")
        for account in get_mt5_accounts()
    ]


DEFAULT_WATCH_SYMBOLS = ["XAUUSD", "BTCUSD", "GBPUSD"]
MAX_WATCH_SYMBOLS = 12


def get_watch_symbols() -> list[str]:
    """Return normalized symbols shown in the live-price panel."""
    raw = load_api_keys().get("watch_symbols", DEFAULT_WATCH_SYMBOLS)
    if not isinstance(raw, list):
        return list(DEFAULT_WATCH_SYMBOLS)
    symbols = []
    for symbol in raw:
        normalized = str(symbol or "").strip().upper()
        if (
            normalized
            and len(normalized) <= 32
            and all(char.isalnum() or char in "._-#" for char in normalized)
            and normalized not in symbols
        ):
            symbols.append(normalized)
    return symbols[:MAX_WATCH_SYMBOLS] or list(DEFAULT_WATCH_SYMBOLS)


def save_watch_symbols(symbols: list[str]) -> None:
    """Persist validated broker symbols used by the live-price panel."""
    if not isinstance(symbols, list):
        raise ValueError("Live-price symbols must be supplied as a list.")
    normalized = []
    for symbol in symbols:
        item = str(symbol or "").strip().upper()
        if not item:
            continue
        if len(item) > 32 or not all(
            char.isalnum() or char in "._-#" for char in item
        ):
            raise ValueError(
                f"Invalid market symbol {item!r}. Use letters, digits, dot, "
                "underscore, or hyphen."
            )
        if item not in normalized:
            normalized.append(item)
    if not normalized:
        raise ValueError("Add at least one symbol to the live-price panel.")
    if len(normalized) > MAX_WATCH_SYMBOLS:
        raise ValueError(f"The live-price panel supports up to {MAX_WATCH_SYMBOLS} symbols.")
    config = load_api_keys()
    config["watch_symbols"] = normalized
    ensure_config_dir()
    CONFIG_FILE.write_text(json.dumps(config, indent=4), encoding="utf-8")


def get_trader_profile() -> dict:
    profile = load_api_keys().get("trader_profile")
    return dict(profile) if isinstance(profile, dict) else {}


def save_trader_profile(profile: dict) -> None:
    """Update the trader's plan without overwriting unrelated preferences."""
    if not isinstance(profile, dict):
        raise ValueError("The trading plan must be a mapping.")
    config = load_api_keys()
    config["trader_profile"] = dict(profile)
    config["trader_profile_setup_complete"] = bool(profile.get("strategy"))
    ensure_config_dir()
    CONFIG_FILE.write_text(json.dumps(config, indent=4), encoding="utf-8")


def save_mt5_accounts(accounts: list[dict], active_account_id: str = "") -> None:
    """Save read-only account metadata and secrets in the OS credential vault."""
    if not isinstance(accounts, list):
        raise ValueError("Trading accounts must be supplied as a list.")
    config = load_api_keys()
    old_accounts = get_mt5_accounts()
    old_ids = {
        str(account.get("id") or f"{account['login']}@{account['server']}")
        for account in old_accounts
    }
    normalized: list[dict] = []
    seen: set[str] = set()
    for account in accounts:
        if not isinstance(account, dict):
            raise ValueError("Each trading account must be a mapping.")
        login = str(account.get("login", "")).strip()
        server = str(account.get("server", "")).strip()
        password = str(account.get("password", "") or "")
        name = str(account.get("name", "") or "").strip()
        if not login and not server and not password:
            continue
        if not login or not server or (not password and
                f"{login}@{server}" not in old_ids):
            raise ValueError(
                "For each account, enter its account number, broker server, "
                "and investor password."
            )
        if not login.isdigit():
            raise ValueError("MT5 account numbers must contain digits only.")
        identifier = f"{login}@{server}"
        if identifier in seen:
            raise ValueError("Each account number and broker server must be unique.")
        seen.add(identifier)
        normalized.append({
            "id": identifier,
            "name": name[:80] or identifier,
            "login": login,
            "server": server,
            "password": password,
        })

    if normalized:
        vault = _keyring()
        try:
            for account in normalized:
                if account["password"]:
                    vault.set_password(
                        _KEYRING_SERVICE,
                        f"mt5:{account['login']}:{account['server']}",
                        account["password"],
                    )
                account.pop("password")
        except vault.errors.KeyringError as exc:
            raise RuntimeError(
                "The OS credential vault could not save the MT5 account credentials."
            ) from exc

    metadata = [
        {key: value for key, value in account.items() if key != "password"}
        for account in normalized
    ]
    if metadata:
        config["mt5_accounts"] = metadata
        selected = str(active_account_id or "").strip()
        config["mt5_active_account_id"] = (
            selected if selected in seen else metadata[0]["id"]
        )
        config["mt5_login"] = metadata[0]["login"]
        config["mt5_server"] = metadata[0]["server"]
    else:
        config.pop("mt5_accounts", None)
        config.pop("mt5_active_account_id", None)
        config.pop("mt5_login", None)
        config.pop("mt5_server", None)

    ensure_config_dir()
    CONFIG_FILE.write_text(json.dumps(config, indent=4), encoding="utf-8")

    removed = old_ids - seen
    if removed:
        vault = _keyring()
        for account in old_accounts:
            identifier = str(account.get("id") or f"{account['login']}@{account['server']}")
            if identifier not in removed:
                continue
            try:
                vault.delete_password(
                    _KEYRING_SERVICE,
                    f"mt5:{account['login']}:{account['server']}",
                )
            except vault.errors.PasswordDeleteError:
                continue
            except vault.errors.KeyringError as exc:
                raise RuntimeError(
                    f"The removed credential for {identifier} could not be deleted "
                    "from the OS vault."
                ) from exc


def save_trader_setup(
    gemini_api_key: str,
    os_name: str,
    profile: dict,
    mt5_login: str = "",
    mt5_server: str = "",
    mt5_investor_password: str = "",
) -> None:
    """Save trader setup, keeping provider secrets in the operating-system vault."""
    login = str(mt5_login or "").strip()
    server = str(mt5_server or "").strip()
    password = str(mt5_investor_password or "")
    mt5_values = (login, server, password)
    if any(mt5_values) and not all(mt5_values):
        raise ValueError("Enter the MT5 account number, server, and investor password together.")
    if login and not login.isdigit():
        raise ValueError("The MT5 account number must contain digits only.")

    config = load_api_keys()
    is_first_setup = not bool(config.get("trader_profile_setup_complete"))
    old_login = str(config.get("mt5_login", "")).strip()
    old_server = str(config.get("mt5_server", "")).strip()
    vault = _keyring()
    try:
        vault.set_password(_KEYRING_SERVICE, "gemini_api_key", gemini_api_key.strip())
        if login:
            vault.set_password(
                _KEYRING_SERVICE, f"mt5:{login}:{server}", password
            )
    except vault.errors.KeyringError as exc:
        raise RuntimeError("The OS credential vault could not save the setup credentials.") from exc
    new_mt5_key = f"mt5:{login}:{server}" if login else ""
    old_mt5_key = f"mt5:{old_login}:{old_server}" if old_login and old_server else ""
    if old_mt5_key and old_mt5_key != new_mt5_key:
        try:
            vault.delete_password(_KEYRING_SERVICE, old_mt5_key)
        except vault.errors.PasswordDeleteError:
            pass
        except vault.errors.KeyringError as exc:
            raise RuntimeError("The previous MT5 credential could not be removed from the OS vault.") from exc

    config.pop("gemini_api_key", None)
    config["gemini_key_in_keyring"] = True
    config["os_system"] = str(os_name)
    config["trader_profile"] = dict(profile)
    config["trader_profile_setup_complete"] = True
    if is_first_setup:
        config["first_setup_introduction_pending"] = True
    config["mt5_login"] = login
    config["mt5_server"] = server
    ensure_config_dir()
    CONFIG_FILE.write_text(json.dumps(config, indent=4), encoding="utf-8")


def is_first_setup_introduction_pending() -> bool:
    """Return whether the one-time post-setup introduction is still pending."""
    config = load_api_keys()
    return bool(
        config.get("trader_profile_setup_complete")
        and config.get("first_setup_introduction_pending")
    )


def mark_first_setup_introduction_sent() -> None:
    """Persist that the one-time post-setup introduction was sent to the model."""
    config = load_api_keys()
    if not config.get("first_setup_introduction_pending"):
        return
    config["first_setup_introduction_pending"] = False
    ensure_config_dir()
    CONFIG_FILE.write_text(json.dumps(config, indent=4), encoding="utf-8")


def is_configured() -> bool:
    key = get_gemini_key()
    return bool(key and len(key) > 15)


def get_assistant_name() -> str:
    """Return the configured assistant name, or 'JARVIS' if not set."""
    return load_api_keys().get("assistant_name", "JARVIS") or "JARVIS"


def get_user_name() -> str:
    """Return the configured user name for addressing."""
    return load_api_keys().get("user_name", "")


def save_assistant_config(assistant_name: str, user_name: str) -> None:
    """Persist assistant name and user name to config."""
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["assistant_name"] = assistant_name.strip() or "JARVIS"
    data["user_name"] = user_name.strip()
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


# ── Assistant voice ──────────────────────────────────────────────────────────
# Gemini Live prebuilt voices. Names are proper nouns — identical in every
# language, so this list is safe to show verbatim in any locale.
AVAILABLE_VOICES = ["Charon", "Puck", "Kore", "Fenrir", "Aoede"]
DEFAULT_VOICE    = "Charon"


def get_voice() -> str:
    """Return the configured Live voice, falling back to the default if unset
    or if the stored value is not a voice we recognise."""
    v = load_api_keys().get("voice_name", DEFAULT_VOICE) or DEFAULT_VOICE
    return v if v in AVAILABLE_VOICES else DEFAULT_VOICE


def save_voice(voice_name: str) -> None:
    """Persist the chosen Live voice. Unknown names collapse to the default so a
    bad value can never reach the API and break the session."""
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    v = (voice_name or "").strip()
    data["voice_name"] = v if v in AVAILABLE_VOICES else DEFAULT_VOICE
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


def get_wake_word_enabled() -> bool:
    """Whether local wake-word gating is on (assistant sleeps until 'Hey Jarvis')."""
    return load_api_keys().get("wake_word_enabled", False)


def save_wake_word_enabled(enabled: bool) -> None:
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["wake_word_enabled"] = bool(enabled)
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


def get_push_to_talk_enabled() -> bool:
    """Hold-a-key-to-speak. When on, the mic is closed unless the chord is held."""
    return load_api_keys().get("push_to_talk_enabled", False)


def save_push_to_talk_enabled(enabled: bool) -> None:
    _save_flag("push_to_talk_enabled", enabled)


HUD_STYLES = ("face", "core")


def get_hud_style() -> str:
    """Which centrepiece the HUD draws: the animated head, or the reactor core.

    Taste, not capability — both render in the same software painter and cost
    about the same. Defaults to the head because that is what TradBot-I shipped
    with; anyone who preferred the older look can switch back in ⚙ and the
    choice survives a restart.
    """
    v = str(load_api_keys().get("hud_style", "face")).strip().lower()
    return v if v in HUD_STYLES else "face"


def save_hud_style(style: str) -> None:
    s = str(style or "").strip().lower()
    _save_flag("hud_style", s if s in HUD_STYLES else "face")


# ── Live-session tuning ──────────────────────────────────────────────────────
# Everything here is optional and has a working default, so an untouched
# config behaves exactly like a configured one. Each value is also a way out:
# if a future model dislikes one of these, set it back and nothing else changes.

def get_thinking_enabled() -> bool:
    """Whether the Live model may spend tokens thinking before it answers.

    Off by default. A voice assistant is judged on how fast it starts talking,
    and the reasoning that actually needs deliberation in this app is delegated
    to the planning tools, which run on a separate non-Live model.
    """
    return bool(load_api_keys().get("thinking_enabled", False))


def save_thinking_enabled(enabled: bool) -> None:
    _save_flag("thinking_enabled", enabled)


def get_turn_tuning() -> dict:
    """How eagerly the server decides you have stopped speaking.

    OFF by default, and that default was earned. Cutting turns shorter looks
    like a free speed win and is not: proactive audio has to judge whether an
    utterance was even addressed to the assistant, and a turn clipped early
    gives it less to judge, so it stays quiet — and the reply to your first
    sentence only arrives once your second one has given it enough context.
    That reads as the assistant being a turn behind, which is far worse than
    the fraction of a second the tuning saves.

    Turn it on with "turn_tuning": {"enabled": true} if your own microphone and
    speaking pace suit it. `silence_ms` is the one that is felt: the pause the
    server sits through before accepting your turn is over.
    """
    cfg = load_api_keys().get("turn_tuning")
    cfg = cfg if isinstance(cfg, dict) else {}

    def _int(key, default, lo, hi):
        try:
            return max(lo, min(hi, int(cfg.get(key, default))))
        except (TypeError, ValueError):
            return default

    return {
        "enabled":    bool(cfg.get("enabled", False)),
        "silence_ms": _int("silence_ms", 550, 200, 3000),
        "prefix_ms":  _int("prefix_ms", 150, 0, 1000),
        # "high" = quicker to decide speech has ended.
        "end_sensitivity":   str(cfg.get("end_sensitivity", "high")).lower(),
        "start_sensitivity": str(cfg.get("start_sensitivity", "default")).lower(),
    }


def save_turn_tuning(values: dict) -> None:
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    cur = data.get("turn_tuning")
    cur = dict(cur) if isinstance(cur, dict) else {}
    cur.update(values or {})
    data["turn_tuning"] = cur
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


def get_proactive_audio_enabled() -> bool:
    """Whether the model gets to decide an utterance was not aimed at it and
    stay quiet.

    On by default — it is what stops the assistant answering the room. But it
    is also the first thing to switch off if replies ever seem to arrive a turn
    late: what looks like lag is usually the model having judged your previous
    sentence as not addressed to it, and only changing its mind once the next
    one arrives.
    """
    return bool(load_api_keys().get("proactive_audio", True))


def save_proactive_audio_enabled(enabled: bool) -> None:
    _save_flag("proactive_audio", enabled)


MEDIA_RESOLUTIONS = ("default", "low", "medium", "high")


def get_media_resolution() -> str:
    """How finely the model tokenises the screenshots and camera frames it is
    sent. 'medium' keeps on-screen text readable at a fraction of the tokens a
    full-resolution frame costs; 'low' is cheaper still but starts losing small
    text, which is most of what screen captures are for."""
    v = str(load_api_keys().get("media_resolution", "medium")).strip().lower()
    return v if v in MEDIA_RESOLUTIONS else "medium"


def save_media_resolution(value: str) -> None:
    v = str(value or "").strip().lower()
    _save_flag("media_resolution", v if v in MEDIA_RESOLUTIONS else "medium")


def _save_flag(key: str, value) -> None:
    """Read-modify-write one key without disturbing the rest of the config."""
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data[key] = bool(value) if isinstance(value, bool) else value
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


def get_brief_enabled() -> bool:
    return load_api_keys().get("morning_brief_enabled", True)


def save_brief_enabled(enabled: bool) -> None:
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["morning_brief_enabled"] = enabled
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


# ── Audio devices ────────────────────────────────────────────────────────────
# Stored as device NAMES, not sounddevice indices. Indices shift every time a
# USB device is plugged in or removed, so a saved index silently starts pointing
# at a different microphone. The empty string means "system default", which is
# both the factory setting and what an unresolvable saved device falls back to —
# so unplugging a headset degrades to the built-in speakers instead of crashing.

def _patch_config(**fields) -> None:
    """Read-modify-write one or more keys in api_keys.json.

    Every setter in this file open-coded this. Collapsing it here means a new
    setting is one line, and there is one place where a corrupt config file is
    handled instead of nine."""
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data.update(fields)
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


def get_input_device() -> str:
    """Microphone device name, or '' for the system default."""
    return (load_api_keys().get("input_device", "") or "").strip()


def save_input_device(name: str) -> None:
    _patch_config(input_device=(name or "").strip())


def get_output_device() -> str:
    """Speaker device name, or '' for the system default."""
    return (load_api_keys().get("output_device", "") or "").strip()


def save_output_device(name: str) -> None:
    _patch_config(output_device=(name or "").strip())


def get_plugin_enabled(plugin_name: str) -> bool:
    """Plugins are enabled by default the moment they're discovered (opt-out model)."""
    return load_api_keys().get("plugins_enabled", {}).get(plugin_name, True)


# ── Per-plugin settings ("tokens" / connection details) ───────────────────────
# Generic store so a plugin can declare its own config fields (PLUGIN_SETTINGS)
# and the settings UI renders + persists them WITHOUT any core edit — keeping the
# drop-in model intact. Values live under plugin_config[<namespace>][<key>].
# A namespace defaults to the plugin name, but a suite of plugins (e.g. the
# several printer plugins) can share ONE namespace.
def get_plugin_config(namespace: str) -> dict:
    """All stored values for a namespace (empty dict if none set yet)."""
    cfg = load_api_keys().get("plugin_config")
    val = cfg.get(namespace) if isinstance(cfg, dict) else None
    return dict(val) if isinstance(val, dict) else {}


def get_plugin_setting(namespace: str, key: str, default=None):
    """A single value from a namespace, or `default` if unset."""
    return get_plugin_config(namespace).get(key, default)


def save_plugin_config(namespace: str, values: dict) -> None:
    """Merge `values` into a namespace's stored config (read-modify-write, like
    every other helper here). Only the provided keys are touched."""
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    pc = data.get("plugin_config")
    if not isinstance(pc, dict):
        pc = {}
    cur = pc.get(namespace)
    if not isinstance(cur, dict):
        cur = {}
    cur.update(values)
    pc[namespace] = cur
    data["plugin_config"] = pc
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


def save_plugin_enabled(plugin_name: str, enabled: bool) -> None:
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    plugins_cfg = data.get("plugins_enabled")
    if not isinstance(plugins_cfg, dict):
        plugins_cfg = {}
    plugins_cfg[plugin_name] = enabled
    data["plugins_enabled"] = plugins_cfg
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")