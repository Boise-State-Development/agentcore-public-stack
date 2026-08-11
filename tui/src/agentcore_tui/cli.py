"""Command-line entrypoint.

``agentcore-tui`` with no subcommand launches the chat UI. The subcommands cover
credential and diagnostic tasks that do not need a full-screen app.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

import httpx

from . import __version__
from .config import (
    ENV_API_KEY,
    ENV_BASE_URL,
    Config,
    config_path,
    delete_key_from_keyring,
    resolve_config,
    save_key_to_keyring,
    write_config_file,
)
from .errors import AgentCoreTuiError
from .logging_setup import (
    ENV_LOG_CONTENT,
    ENV_LOG_LEVEL,
    configure_logging,
    content_logging_enabled,
    default_log_path,
    log_path,
)

#: Health endpoint used by `status` — unauthenticated, so it costs nothing.
HEALTH_PATH = "/health"


def _build_parser() -> argparse.ArgumentParser:
    # Shared options live on a parent parser so they work both before and after
    # a subcommand (`--config x status` and `status --config x`).
    #
    # `default=argparse.SUPPRESS` is what makes that safe: without it the
    # subparser would re-set each dest to None and clobber a value that was
    # given before the subcommand. With SUPPRESS the attribute is simply not
    # written when the flag is absent, so the baseline from `set_defaults`
    # below (or the earlier occurrence) survives.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--base-url", dest="base_url", default=argparse.SUPPRESS, help="app-api base URL, e.g. https://your-host/api")
    common.add_argument("--model", dest="model_id", default=argparse.SUPPRESS, help="Bedrock model ID to use for this session")
    common.add_argument(
        "--config",
        dest="config_file",
        type=Path,
        default=argparse.SUPPRESS,
        help=f"Config file to use (default: {config_path()})",
    )
    common.add_argument(
        "--log-level",
        dest="log_level",
        default=argparse.SUPPRESS,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help=f"Log verbosity (default: INFO, or ${ENV_LOG_LEVEL})",
    )
    common.add_argument(
        "--log-file",
        dest="log_file",
        type=Path,
        default=argparse.SUPPRESS,
        help=f"Log file (default: {default_log_path()})",
    )

    parser = argparse.ArgumentParser(
        prog="agentcore-tui",
        parents=[common],
        description="Terminal client for the AgentCore platform. Run with no arguments to start chatting.",
    )
    parser.add_argument("--version", action="version", version=f"agentcore-tui {__version__}")
    # NB: no `parser.set_defaults()` for the shared dests. `set_defaults`
    # rewrites `action.default` on every matching action, and `parents=[common]`
    # shares those Action *instances* with the subparsers — so setting a default
    # here would replace SUPPRESS with None and reintroduce the clobbering this
    # is designed to avoid. `_normalise` fills the gaps after parsing instead.

    subparsers = parser.add_subparsers(dest="command")

    login = subparsers.add_parser("login", parents=[common], help="Store an API key in the OS keyring")
    login.add_argument(
        "--api-key",
        dest="api_key",
        default=None,
        help="API key value. Omit to be prompted without echo, which keeps it out of your shell history.",
    )

    subparsers.add_parser("logout", parents=[common], help="Remove the stored API key")
    subparsers.add_parser("status", parents=[common], help="Show resolved configuration and check connectivity")

    return parser


def _print_error(exc: AgentCoreTuiError) -> None:
    print(f"error: {exc.message}", file=sys.stderr)
    if exc.hint:
        print(f"  {exc.hint}", file=sys.stderr)


def _command_login(args: argparse.Namespace) -> int:
    config = resolve_config(base_url=args.base_url, config_file=args.config_file, use_keyring=False)
    if not config.base_url:
        print(
            f"error: no base URL. Pass --base-url, or set {ENV_BASE_URL}.",
            file=sys.stderr,
        )
        return 2

    api_key = args.api_key
    if not api_key:
        print(f"Paste the API key for {config.base_url} (input hidden).")
        print("Create one in the web app under Settings -> API Keys; it is shown only once.")
        try:
            api_key = getpass.getpass("API key: ")
        except (EOFError, KeyboardInterrupt):
            print("\naborted", file=sys.stderr)
            return 130

    api_key = api_key.strip()
    if not api_key:
        print("error: empty API key", file=sys.stderr)
        return 2

    save_key_to_keyring(config.base_url, api_key)
    written = write_config_file({"base_url": config.base_url}, args.config_file)
    print(f"Stored key for {config.base_url} in the OS keyring.")
    print(f"Wrote base URL to {written}.")
    print("Run `agentcore-tui` to start chatting.")
    return 0


def _command_logout(args: argparse.Namespace) -> int:
    config = resolve_config(base_url=args.base_url, config_file=args.config_file, use_keyring=False)
    if not config.base_url:
        print(f"error: no base URL. Pass --base-url, or set {ENV_BASE_URL}.", file=sys.stderr)
        return 2
    if delete_key_from_keyring(config.base_url):
        print(f"Removed the stored key for {config.base_url}.")
        return 0
    print(f"No stored key found for {config.base_url}.")
    return 1


def _describe_key(config: Config) -> str:
    if not config.api_key:
        return "missing"
    source = "config file (plain text)" if config.api_key_from_plaintext_file else "env or keyring"
    # Never print the key itself, not even partially.
    return f"present ({len(config.api_key)} chars, from {source})"


def _command_status(args: argparse.Namespace) -> int:
    config = resolve_config(base_url=args.base_url, model_id=args.model_id, config_file=args.config_file)
    print(f"agentcore-tui {__version__}")
    print(f"config file : {args.config_file or config_path()}")
    print(f"base URL    : {config.base_url or 'missing'}")
    print(f"API key     : {_describe_key(config)}")
    print(f"model       : {config.model_id}")
    print(f"max tokens  : {config.max_tokens:,}")
    print(f"log file    : {log_path(args.log_file)}")
    print(f"log content : {'yes' if content_logging_enabled() else f'no (set {ENV_LOG_CONTENT}=1)'}")
    if config.keyring_unavailable_reason:
        print(f"keyring     : unavailable ({config.keyring_unavailable_reason})")
        print(f"              set {ENV_API_KEY} in your environment instead")

    if not config.base_url:
        return 1

    url = f"{config.base_url.rstrip('/')}{HEALTH_PATH}"
    try:
        response = httpx.get(url, timeout=10.0, follow_redirects=True)
        print(f"health      : HTTP {response.status_code} from {url}")
    except httpx.HTTPError as exc:
        print(f"health      : unreachable ({type(exc).__name__}: {exc})")
        return 1

    return 0 if config.is_complete else 1


def _command_chat(args: argparse.Namespace) -> int:
    # Imported lazily so `login`/`status` do not pay Textual's import cost.
    from .app import ChatApp

    config = resolve_config(base_url=args.base_url, model_id=args.model_id, config_file=args.config_file)
    ChatApp(config).run()
    return 0


def _normalise(args: argparse.Namespace) -> argparse.Namespace:
    """Fill in attributes that ``argparse.SUPPRESS`` left unset.

    Shared flags default to SUPPRESS so a subcommand cannot overwrite a value
    given before it; the cost is that absent flags have no attribute at all.
    """
    for name in ("base_url", "model_id", "config_file", "api_key", "command", "log_level", "log_file"):
        if not hasattr(args, name):
            setattr(args, name, None)
    return args


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``agentcore-tui`` console script."""
    parser = _build_parser()
    args = _normalise(parser.parse_args(argv))

    # Configure before anything else so startup failures are captured. Logging
    # goes to a file, never the terminal — this is a full-screen app.
    configure_logging(level=args.log_level, path=args.log_file)

    handlers = {
        "login": _command_login,
        "logout": _command_logout,
        "status": _command_status,
        None: _command_chat,
    }
    handler = handlers[args.command]

    try:
        return handler(args)
    except AgentCoreTuiError as exc:
        _print_error(exc)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
