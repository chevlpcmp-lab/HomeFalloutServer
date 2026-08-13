#!/usr/bin/env python3
"""Syntax-check every Python script embedded in a ConfigMap block scalar.

The media-stack bootstrap lives inside a ConfigMap YAML, so an indentation slip
or syntax error only surfaces at pod start. This extracts each `<name>.py: |`
block from every ConfigMap under platform/components and compiles it.
"""
import pathlib
import py_compile
import re
import sys
import tempfile

root = pathlib.Path(__file__).resolve().parent.parent
failures = 0
checked = 0

for path in sorted((root / "platform" / "components").rglob("*.yaml")):
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    if "kind: ConfigMap" not in text:
        continue
    for match in re.finditer(r"^  ([\w.-]+\.py): \|\n((?:    .*\n|\n)+)", text, re.MULTILINE):
        name, block = match.groups()
        rel = path.relative_to(root)
        lines = []
        bad_indent = None
        for line in block.splitlines():
            if not line.strip():
                lines.append("")
            elif line.startswith("    "):
                lines.append(line[4:])
            else:
                bad_indent = line
                break
        if bad_indent is not None:
            print(f"FAIL {rel} :: {name}: line breaks the 4-space block indent: {bad_indent!r}")
            failures += 1
            continue
        checked += 1
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
            tmp = handle.name
        try:
            py_compile.compile(tmp, doraise=True)
            print(f"OK   {rel} :: {name} ({len(lines)} lines)")
        except py_compile.PyCompileError as error:
            print(f"FAIL {rel} :: {name}\n{error}")
            failures += 1

if checked == 0 and failures == 0:
    print("no embedded python found under platform/components")

sys.exit(1 if failures else 0)
