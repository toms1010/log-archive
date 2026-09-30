"""Long help text, kept out of :mod:`log_archive.cli` so the parser stays readable."""

from __future__ import annotations

EXAMPLES = """\
examples:
  log-archive /var/log                          create an archive
  log-archive archive /var/log -o ~/archives    choose the destination
  log-archive /var/log -x "*.log.1"             exclude rotated logs
  log-archive ~/logs --dry-run                  preview without writing
  log-archive /var/log --verify --checksum      verify and write a SHA-256 file
  log-archive /var/log --keep 10                archive, then keep the newest 10
  log-archive list                              show archives in the destination
  log-archive info ARCHIVE.tar.gz               show details about an archive
  log-archive verify ARCHIVE.tar.gz             check an archive is intact
  log-archive cleanup --keep 10                 apply retention
  sudo log-archive /var/log                     archive a system log directory

exit codes:
  0 success   1 error     2 usage      3 permission   4 archive error
  5 verification failed   6 config     7 checksum     8 cleanup error
  130 interrupted
"""
