import os
import re
from app.adapters.tools.base import BaseTool
from app.adapters.tools.registry import register_tool


@register_tool
class EditTool(BaseTool):
    name = "edit"
    description = (
        "Replace an exact string in an existing file. old_string must match the "
        "file exactly (including whitespace/indentation) and, unless replace_all "
        "is true, must be unique — otherwise the edit is rejected so you can add "
        "more surrounding context. Escape sequences that appear literally in "
        "source code (e.g. \\n, \\x89 inside a string literal) must be sent with "
        "doubled backslashes in the JSON arguments."
    )

    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Path of the file to modify.",
                },
                "old_string": {
                    "type": "string",
                    "description": "The exact text to replace.",
                },
                "new_string": {
                    "type": "string",
                    "description": "The text to replace it with (must differ from old_string).",
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace every occurrence instead of requiring a unique match.",
                    "default": False,
                },
            },
            "required": ["file_path", "old_string", "new_string"],
        }

    async def execute(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> str:
        file_path = self.resolve_path(file_path)
        if not os.path.isfile(file_path):
            return f"[Error] File not found: {file_path}"
        if old_string == new_string:
            return "[Error] old_string and new_string are identical — nothing to change."
        if old_string == "":
            return "[Error] old_string is empty — use the write tool to create a file."

        # newline="" keeps the file byte-exact: no CRLF->LF on read and no
        # LF->CRLF on write (Windows text mode would rewrite every line ending).
        try:
            with open(file_path, "r", encoding="utf-8", newline="") as f:
                content = f.read()
        except OSError as e:
            return f"[Error] Could not read {file_path}: {e}"

        # The read tool strips "\r", so for CRLF files the model sends "\n";
        # convert both strings to the file's line-ending convention.
        if "\r\n" in content and "\r\n" not in old_string:
            old_string = old_string.replace("\n", "\r\n")
            new_string = new_string.replace("\n", "\r\n")

        count = content.count(old_string)
        if count == 0:
            return self._fallback_escaped(
                file_path, content, old_string, new_string, replace_all
            )
        if count > 1 and not replace_all:
            return _not_unique(file_path, count)

        new_content = (
            content.replace(old_string, new_string)
            if replace_all
            else content.replace(old_string, new_string, 1)
        )
        return _write(file_path, new_content, count if replace_all else 1)

    @staticmethod
    def _fallback_escaped(
        file_path: str,
        content: str,
        old_string: str,
        new_string: str,
        replace_all: bool,
    ) -> str:
        """Recover from a common model mistake.

        Source text such as ``b"\\x89PNG\\r\\n"`` is sometimes sent with single
        JSON escaping, so old_string arrives holding real control characters
        (0x89, CR, LF, 0x1A) instead of backslash sequences. Retry with each
        control character also allowed to match its escaped spelling.
        """
        not_found = f"[Error] old_string not found in {file_path}."
        if not any(_is_suspicious(c) for c in old_string):
            return not_found

        pattern = re.compile("".join(_char_pattern(c) for c in old_string))
        matches = list(pattern.finditer(content))
        if not matches:
            return not_found + _CTRL_HINT
        if len(matches) > 1 and not replace_all:
            return _not_unique(file_path, len(matches))
        # If new_string has the same mis-escaping we can't tell which raw
        # characters were meant as escape text, so refuse rather than guess.
        if any(_is_suspicious(c) for c in new_string):
            return (
                f"[Error] old_string not found in {file_path} as given. It only "
                "matches if its control characters are read as backslash escape "
                "sequences, and new_string also contains raw control characters, "
                "so the intended replacement is ambiguous." + _CTRL_HINT
            )

        targets = matches if replace_all else matches[:1]
        pieces, last = [], 0
        for m in targets:
            pieces.append(content[last:m.start()])
            pieces.append(new_string)
            last = m.end()
        pieces.append(content[last:])
        return _write(
            file_path,
            "".join(pieces),
            len(targets),
            " [note: old_string contained raw control characters; they were "
            "matched against backslash escape sequences in the file]",
        )


_CTRL_HINT = (
    " old_string contains raw control characters (e.g. CR, \\x1a, \\x89). If the "
    "file contains escape sequences like \\r or \\x89 as literal text, double "
    "the backslashes in the JSON arguments."
)

_NAMED_ESCAPES = {
    "\n": "\\n", "\r": "\\r", "\t": "\\t", "\0": "\\0", "\a": "\\a",
    "\b": "\\b", "\f": "\\f", "\v": "\\v", "\x1b": "\\e",
}


def _is_ctrl(c: str) -> bool:
    o = ord(c)
    return o < 0x20 or 0x7F <= o <= 0x9F


def _is_suspicious(c: str) -> bool:
    # Plain LF and tab are normal in code; anything else is a likely sign of
    # an escape sequence that was decoded once too often.
    return _is_ctrl(c) and c not in "\n\t"


def _char_pattern(c: str) -> str:
    if not _is_ctrl(c):
        return re.escape(c)
    o = ord(c)
    alts = [f"\\x{o:02x}", f"\\x{o:02X}", f"\\u{o:04x}", f"\\u{o:04X}"]
    if c in _NAMED_ESCAPES:
        alts.insert(0, _NAMED_ESCAPES[c])
    alts = [re.escape(a) for a in alts]
    alts.insert(0, re.escape(c))
    if c == "\n":
        alts.insert(0, "\r\n")
    return "(?:" + "|".join(alts) + ")"


def _not_unique(file_path: str, count: int) -> str:
    return (
        f"[Error] old_string is not unique ({count} matches in {file_path}). "
        "Add surrounding context to disambiguate, or set replace_all=true."
    )


def _write(file_path: str, new_content: str, n: int, note: str = "") -> str:
    try:
        with open(file_path, "w", encoding="utf-8", newline="") as f:
            f.write(new_content)
    except OSError as e:
        return f"[Error] Could not write {file_path}: {e}"
    return f"[Edited] {file_path} ({n} replacement(s)){note}"
