from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
import json
import os
from pathlib import Path
import shutil
import sys
from typing import TextIO

from . import __version__
from .clients import ClaudeAdapter, ClientInstallError, CodexAdapter, CursorAdapter
from .doctor import collect_diagnostics
from .embedded import EmbeddedPayloadError, payload_source
from .platforms import data_root
from .receipts import ReceiptError, load_receipt


CLIENT_NAMES = ("claude", "codex", "cursor")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nightfalcon")
    subcommands = parser.add_subparsers(dest="command")
    subcommands.add_parser("version", help="show installed NightFalcon version")

    detect = subcommands.add_parser("detect", help="detect supported agent clients")
    detect.add_argument("--json", action="store_true", dest="as_json")

    doctor = subcommands.add_parser("doctor", help="verify clients and managed installs")
    doctor.add_argument("--json", action="store_true", dest="as_json")

    install = subcommands.add_parser("install", help="install a client distribution")
    install.add_argument("client", choices=(*CLIENT_NAMES, "all"))
    install.add_argument("--target", type=Path)
    install.add_argument("--yes", action="store_true")
    install.add_argument("--force", action="store_true")

    uninstall = subcommands.add_parser("uninstall", help="remove a verified managed install")
    uninstall.add_argument("client", choices=CLIENT_NAMES)
    uninstall.add_argument("--yes", action="store_true")
    return parser


def _adapters(root: Path, which: Callable[[str], str | None]):
    return {
        "claude": ClaudeAdapter(root=root, which=which),
        "codex": CodexAdapter(root=root, which=which),
        "cursor": CursorAdapter(root=root, which=which),
    }


def _confirm(
    message: str,
    *,
    yes: bool,
    stdin_isatty: bool,
    prompt: Callable[[str], str],
    stderr: TextIO,
) -> bool | None:
    if yes:
        return True
    if not stdin_isatty:
        print("nightfalcon: non-interactive mutation requires --yes", file=stderr)
        return None
    return prompt(f"{message} [y/N] ").strip().casefold() in {"y", "yes"}


def _render_detections(adapters, *, as_json: bool, stdout: TextIO) -> list[str]:
    detections = [adapters[name].detect() for name in CLIENT_NAMES]
    if as_json:
        print(
            json.dumps(
                {
                    "clients": [
                        {
                            "client": item.client,
                            "available": item.available,
                            "executable": str(item.executable) if item.executable else None,
                            "detail": item.detail,
                        }
                        for item in detections
                    ]
                },
                sort_keys=True,
            ),
            file=stdout,
        )
    else:
        for item in detections:
            marker = "found" if item.available else "not found"
            print(f"{item.client}: {marker}", file=stdout)
    return [item.client for item in detections if item.available]


def _install_clients(
    clients: Sequence[str],
    *,
    adapters,
    root: Path,
    target: Path | None,
    force: bool,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    for client in clients:
        destination = target if client == "cursor" and target else root / "versions" / __version__ / client
        try:
            with payload_source(client) as source:
                plan = adapters[client].plan(source, destination)
                receipt = adapters[client].install(plan, force=force)
        except (ClientInstallError, EmbeddedPayloadError, OSError, ValueError) as error:
            print(f"nightfalcon: {client} install failed: {error}", file=stderr)
            return 1
        print(f"Installed {client} {receipt.version} at {receipt.destination}", file=stdout)
        for instruction in plan.instructions:
            print(instruction, file=stdout)
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    platform: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
    stdin_isatty: bool | None = None,
    prompt: Callable[[str], str] = input,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    args = _parser().parse_args(argv)
    environment = os.environ if env is None else env
    active_platform = sys.platform if platform is None else platform
    output = sys.stdout if stdout is None else stdout
    errors = sys.stderr if stderr is None else stderr
    interactive = sys.stdin.isatty() if stdin_isatty is None else stdin_isatty

    if args.command == "version":
        print(f"nightfalcon {__version__}", file=output)
        return 0

    try:
        root = data_root(environment, active_platform)
    except ValueError as error:
        print(f"nightfalcon: {error}", file=errors)
        return 2
    adapters = _adapters(root, which)

    if args.command == "detect":
        _render_detections(adapters, as_json=args.as_json, stdout=output)
        return 0

    if args.command == "doctor":
        report = collect_diagnostics(root, adapters)
        if args.as_json:
            print(json.dumps(report, sort_keys=True), file=output)
        else:
            for item in report["clients"]:
                print(f"{item['client']}: {item['detail']}", file=output)
            for item in report["installations"]:
                print(f"{item['client']} {item['version']}: {item['status']}", file=output)
        return 0 if all(item["status"] == "ok" for item in report["installations"]) else 1

    if args.command == "uninstall":
        confirmed = _confirm(
            f"Remove managed {args.client} installation?",
            yes=args.yes,
            stdin_isatty=interactive,
            prompt=prompt,
            stderr=errors,
        )
        if confirmed is None:
            return 2
        if not confirmed:
            return 0
        receipt_path = root / "versions" / __version__ / f"{args.client}.receipt.json"
        try:
            receipt = load_receipt(receipt_path)
            adapters[args.client].uninstall(receipt)
        except (ReceiptError, ClientInstallError, OSError) as error:
            print(f"nightfalcon: uninstall failed: {error}", file=errors)
            return 1
        print(f"Removed managed {args.client} installation", file=output)
        return 0

    if args.command == "install":
        confirmed = _confirm(
            f"Install NightFalcon for {args.client}?",
            yes=args.yes,
            stdin_isatty=interactive,
            prompt=prompt,
            stderr=errors,
        )
        if confirmed is None:
            return 2
        if not confirmed:
            return 0
        clients = CLIENT_NAMES if args.client == "all" else (args.client,)
        return _install_clients(
            clients,
            adapters=adapters,
            root=root,
            target=args.target,
            force=args.force,
            stdout=output,
            stderr=errors,
        )

    available = _render_detections(adapters, as_json=False, stdout=output)
    if not interactive:
        print(
            "nightfalcon: non-interactive invocation requires an explicit command",
            file=errors,
        )
        return 2
    if not available:
        print("No supported clients detected.", file=output)
        return 1
    confirmed = _confirm(
        "Install NightFalcon for detected clients?",
        yes=False,
        stdin_isatty=True,
        prompt=prompt,
        stderr=errors,
    )
    if not confirmed:
        return 0
    return _install_clients(
        available,
        adapters=adapters,
        root=root,
        target=None,
        force=False,
        stdout=output,
        stderr=errors,
    )


def entrypoint() -> int:
    return main()
