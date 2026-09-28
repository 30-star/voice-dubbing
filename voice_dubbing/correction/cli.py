"""Explicit draft/save/discard operations for the ASR correction directory."""

import json
from pathlib import Path

from ..errors import ValidationError
from ..timeline import load_timeline
from .store import (discard_correction, edit_correction, initialize_correction, inspect_correction,
                    save_correction)


def add_correction_commands(subcommands) -> None:
    parser = subcommands.add_parser("correction", help="review and correct ASR baseline subtitles")
    commands = parser.add_subparsers(dest="correction_command", required=True)
    init = commands.add_parser("init", help="import a legacy Timeline into an empty review directory")
    init.add_argument("timeline", type=Path)
    init.add_argument("--output-dir", type=Path, required=True)
    for name in ("set-text", "inspect", "save", "discard", "restore", "restore-all"):
        command = commands.add_parser(name)
        command.add_argument("subtitle_dir", type=Path)
        if name in {"set-text", "restore"}:
            command.add_argument("--segment-id", required=True)
        if name == "set-text":
            command.add_argument("--text", required=True)


def run_correction_command(args) -> int:
    name = args.correction_command
    if name == "init":
        folder = args.output_dir.resolve()
        if folder.exists() and (not folder.is_dir() or any(folder.iterdir())):
            raise ValidationError("correction init output directory must be empty")
        initialize_correction(load_timeline(args.timeline), folder)
    else:
        folder = args.subtitle_dir.resolve()
        if name == "set-text":
            edit_correction(folder, segment_id=args.segment_id, text=args.text)
        elif name == "restore":
            edit_correction(folder, segment_id=args.segment_id, restore=True)
        elif name == "restore-all":
            edit_correction(folder, all_segments=True)
        elif name == "save":
            save_correction(folder)
        elif name == "discard":
            discard_correction(folder)
    print(json.dumps(inspect_correction(folder), ensure_ascii=False))
    return 0
