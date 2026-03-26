"""Parse unified locate strings: file:line:col"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class Location:
    """A parsed file:line:col location."""

    file: str
    line: int
    col: int

    @classmethod
    def parse(cls, locate_str: str) -> "Location":
        """Parse a locate string like 'file.rs:42:10' into a Location.

        All values are 1-indexed (matching editor/compiler conventions).

        Supports:
          file.rs:42:10  -> file, line 42, col 10
          file.rs:42     -> file, line 42, col 1 (default)
        """
        parts = locate_str.rsplit(":", 2)
        if len(parts) == 3:
            file_path, line_str, col_str = parts
            # Handle Windows absolute paths like C:\foo\bar.rs:42:10
            # where rsplit(":") would split the drive letter too
            if len(file_path) == 1 and file_path.isalpha():
                # Reconstructing: C + : + rest_was_split_wrong
                # Re-parse more carefully
                return cls._parse_windows(locate_str)
            return cls(
                file=file_path,
                line=int(line_str),
                col=int(col_str),
            )
        elif len(parts) == 2:
            file_path, line_str = parts
            if len(file_path) == 1 and file_path.isalpha():
                return cls._parse_windows(locate_str)
            return cls(
                file=file_path,
                line=int(line_str),
                col=1,
            )
        else:
            raise ValueError(
                f"Invalid locate string: {locate_str!r}. "
                "Expected format: file:line:col or file:line"
            )

    @classmethod
    def _parse_windows(cls, locate_str: str) -> "Location":
        """Handle Windows paths like C:\\foo\\bar.rs:42:10"""
        # Find the drive letter prefix (e.g., "C:")
        if len(locate_str) >= 2 and locate_str[1] == ":":
            rest = locate_str[2:]  # everything after "C:"
            parts = rest.rsplit(":", 2)
            if len(parts) == 3:
                path_rest, line_str, col_str = parts
                return cls(
                    file=locate_str[:2] + path_rest,
                    line=int(line_str),
                    col=int(col_str),
                )
            elif len(parts) == 2:
                path_rest, line_str = parts
                return cls(
                    file=locate_str[:2] + path_rest,
                    line=int(line_str),
                    col=1,
                )
        raise ValueError(
            f"Invalid locate string: {locate_str!r}. "
            "Expected format: file:line:col or file:line"
        )

    def resolve_relative(self, root: str) -> str:
        """Return the file path relative to root, or as-is if already relative."""
        abs_path = os.path.abspath(self.file)
        abs_root = os.path.abspath(root)
        try:
            return os.path.relpath(abs_path, abs_root)
        except ValueError:
            # Different drives on Windows
            return self.file
