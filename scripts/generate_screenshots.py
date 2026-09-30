#!/usr/bin/env python3
"""Regenerate the README screenshots in ``docs/screenshots/``.

Every screenshot shows *real* captured output from the tool. The demo log tree
is entirely synthetic and the sandbox paths are rewritten for presentation, so
no real hostnames, usernames, IP addresses, or log contents can leak into the
repository.

Usage::

    pip install ".[dev]" pillow
    python scripts/generate_screenshots.py
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = REPO_ROOT / "docs" / "screenshots"
SANDBOX = Path("/var/tmp/log-archive-demo")

#: Sandbox paths are swapped for these in the rendered images, so the
#: screenshots read like a real server without exposing this machine.
PATH_SUBSTITUTIONS = (
    (str(SANDBOX / "var-log"), "/var/log"),
    (str(SANDBOX / "home"), "/home/demo"),
)

FONT_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"

FONT_SIZE = 15
LINE_HEIGHT = 22
CHAR_PADDING = 26
TOP_CHROME = 42
BOTTOM_PADDING = 18
MIN_COLUMNS = 92

# A calm, high contrast dark palette that stays readable on GitHub in both
# light and dark mode.
BG = (24, 26, 32)
CHROME = (36, 39, 47)
FG = (222, 226, 233)
DIM = (140, 148, 162)
PROMPT = (126, 209, 130)
OK = (126, 209, 130)
WARN = (233, 189, 112)
ERR = (240, 130, 130)
HEADING = (140, 180, 240)


@dataclass
class Line:
    """One rendered row of terminal output."""

    text: str
    color: tuple[int, int, int] = FG
    bold: bool = False


def build_demo_tree() -> None:
    """Create a realistic but entirely synthetic log directory."""
    if SANDBOX.exists():
        shutil.rmtree(SANDBOX)

    root = SANDBOX / "var-log"
    services = [
        "nginx",
        "apache2",
        "sshd",
        "cron",
        "systemd",
        "docker",
        "containerd",
        "postgresql",
        "mysql",
        "redis",
        "kernel",
        "auditd",
    ]

    for service in services:
        directory = root / service
        directory.mkdir(parents=True)
        for rotation in range(11):
            name = "service.log" if rotation == 0 else f"service.log.{rotation}"
            (directory / name).write_text(
                f"{'2026-09-30 07:5' + str(rotation % 10)}:00:00 [INFO] {service}: "
                f"request handled in {12 + rotation}ms\n" * 40
            )

    (root / "syslog").write_text("Sep 30 07:00:01 host systemd[1]: Started session.\n" * 60)
    (root / "messages").write_text("Sep 30 07:00:02 host CRON[2211]: session opened\n" * 45)
    (root / "dmesg").write_text("[    0.000000] Linux version 6.8.0\n" * 30)
    (root / "auth.log").write_text("Accepted publickey for deploy\n" * 25)
    (root / "boot.log").write_text("systemd[1]: Reached target multi-user\n" * 20)

    # Entries the tool is expected to exclude or skip, so the screenshots show
    # the real categories in the report.
    (root / "journal").mkdir()
    (root / "journal" / "system.journal").write_bytes(b"\x00" * 2048)
    (root / "nginx" / "access.log.1.gz").write_bytes(b"\x1f\x8b" + b"\x00" * 512)
    (root / "nginx" / "access.log.2.gz").write_bytes(b"\x1f\x8b" + b"\x00" * 512)
    (root / "app.pid").write_text("2043\n")
    (root / "docker.sock").touch()
    os.mkfifo(root / "audit.pipe")
    (root / "wtmp").write_bytes(b"\x00" * 64)

    # A file the current user cannot read, to demonstrate that an unreadable
    # file is skipped instead of failing the run.
    restricted = root / "systemd" / "private"
    restricted.write_text("root only detail\n")
    restricted.chmod(0o000)

    (SANDBOX / "home").mkdir(parents=True, exist_ok=True)


def run_tool(args: list[str]) -> str:
    """Run the installed CLI inside the sandbox and capture its output.

    A non-zero exit is reported loudly: a screenshot that silently captured a
    failure would be worse than no screenshot at all.
    """
    env = dict(os.environ)
    env["HOME"] = str(SANDBOX / "home")
    env["COLUMNS"] = "200"
    result = subprocess.run(
        [sys.executable, "-m", "log_archive", *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO_ROOT,
        check=False,
    )
    combined = result.stdout + result.stderr
    if result.returncode != 0:
        print(f"  ! log-archive {' '.join(args)} exited {result.returncode}")
        print("  " + combined.replace("\n", "\n  "), file=sys.stderr)
        raise SystemExit(f"refusing to screenshot a failed run: {' '.join(args)}")
    return combined


def sanitise(text: str) -> str:
    """Replace sandbox paths with realistic-looking, non-identifying ones."""
    for real, pretty in PATH_SUBSTITUTIONS:
        text = text.replace(real, pretty)
    # The banner renders whatever hostname the build machine has.
    return re.sub(r"Source      /var/log.*", "Source      /var/log", text)


def style_line(text: str) -> Line:
    """Pick a colour for one line of tool output."""
    stripped = text.strip()
    if stripped.startswith("Error") or stripped.startswith("✗"):
        return Line(text, ERR, True)
    if stripped.startswith("Warning"):
        return Line(text, WARN)
    if stripped.startswith("✓"):
        return Line(text, OK, True)
    if stripped.startswith(
        ("DRY RUN", "Cleanup preview", "Archive Information", "Verifying archive")
    ):
        return Line(text, HEADING, True)
    if stripped.startswith("✗"):
        return Line(text, ERR, True)
    return Line(text, FG)


def build_lines(command: str, output: str) -> list[Line]:
    """Interleave the typed command with its output, styled.

    Both the command and its output are sanitised, so a path can never leak
    through by appearing in the command rather than the response.
    """
    lines = [Line(f"$ {sanitise(command)}", PROMPT, True)]
    for raw in sanitise(output).rstrip("\n").split("\n"):
        lines.append(style_line(raw))
    return lines


def build_session(steps: list[tuple[str, str]]) -> list[Line]:
    """Interleave several commands and their outputs into one window."""
    lines: list[Line] = []
    for command, output in steps:
        lines.extend(build_lines(sanitise(command), output))
        lines.append(Line(""))
    return lines


def render(lines: list[Line], title: str, destination: Path) -> None:
    """Draw the terminal window and write a PNG."""
    regular = ImageFont.truetype(FONT_REGULAR, FONT_SIZE)
    bold_font = ImageFont.truetype(FONT_BOLD, FONT_SIZE)
    title_font = ImageFont.truetype(FONT_REGULAR, 13)

    # DejaVu Sans Mono advances 0.602 em per character.
    char_width = regular.getlength("M")
    columns = max(MIN_COLUMNS, max(len(line.text) for line in lines) + 2)
    width = int(columns * char_width) + CHAR_PADDING * 2
    height = TOP_CHROME + len(lines) * LINE_HEIGHT + BOTTOM_PADDING

    image = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(image)

    # Window chrome.
    draw.rectangle([0, 0, width, TOP_CHROME], fill=CHROME)
    for index, colour in enumerate(((255, 95, 86), (255, 189, 46), (39, 201, 63))):
        centre_x = 20 + index * 19
        draw.ellipse(
            [centre_x - 6, TOP_CHROME // 2 - 6, centre_x + 6, TOP_CHROME // 2 + 6],
            fill=colour,
        )
    title_width = title_font.getlength(title)
    draw.text(
        ((width - title_width) / 2, (TOP_CHROME - 15) / 2),
        title,
        font=title_font,
        fill=DIM,
    )

    y = TOP_CHROME + 6
    for line in lines:
        if line.text:
            draw.text(
                (CHAR_PADDING, y),
                line.text,
                font=bold_font if line.bold else regular,
                fill=line.color,
            )
        y += LINE_HEIGHT

    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination, optimize=True)
    print(f"wrote {destination.relative_to(REPO_ROOT)}  ({width}x{height})")


def newest_archive(archive_dir: Path) -> Path:
    """Return the most recent real archive in *archive_dir*.

    Uses the tool's own name matcher so a ``.sha256`` sidecar, which sorts
    after the archive it belongs to, is never mistaken for the archive.
    """
    from log_archive.retention import is_owned_archive

    found = sorted(p for p in archive_dir.iterdir() if is_owned_archive(p))
    if not found:
        raise SystemExit(f"no archive found in {archive_dir}")
    return found[-1]


def main() -> int:
    build_demo_tree()
    logs = SANDBOX / "var-log"
    archives = SANDBOX / "home" / "log-archives"

    def shoot(name: str, title: str, args: list[str]) -> None:
        """Run one command, then render it as a PNG."""
        output = run_tool(args)
        display = " ".join(["log-archive", *args])
        render(build_lines(display, output), title, OUTPUT_DIR / f"{name}.png")

    # These must run in order: later steps depend on archives that earlier
    # steps create.
    shoot("01-help", "log-archive --help", ["--help"])
    shoot("03-dry-run", "log-archive /var/log --dry-run", [str(logs), "--dry-run"])
    shoot("02-archive", "log-archive /var/log --verify", [str(logs), "--verify"])
    shoot("04-verify", "log-archive verify", ["verify", str(newest_archive(archives))])
    shoot("05-checksum", "log-archive /var/log --checksum", [str(logs), "--checksum"])
    shoot("06-info", "log-archive info", ["info", str(newest_archive(archives))])

    # Retention needs several archives to be worth showing. Preview and
    # execution are captured together, because the preview is what makes the
    # "only our own files" guarantee visible.
    for _ in range(4):
        run_tool([str(logs), "-q"])
    preview = ["cleanup", "-o", str(archives), "--keep", "3", "--dry-run"]
    apply_it = ["cleanup", "-o", str(archives), "--keep", "3"]
    session = [
        (f"log-archive {' '.join(preview)}", run_tool(preview)),
        (f"log-archive {' '.join(apply_it)}", run_tool(apply_it)),
    ]
    render(
        build_session(session),
        "log-archive cleanup --keep 3",
        OUTPUT_DIR / "07-cleanup.png",
    )

    print(f"\nscreenshots written to {OUTPUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
