#!/usr/bin/env python3
"""Genera app/schemas/unicode_props.py desde la UCD 16.0.0 vendida.

Uso: uv run python scripts/generate_unicode_tables.py

Fuentes: DerivedJoiningType.txt, emoji-data.txt, DerivedGeneralCategory.txt,
emoji-zwj-sequences.txt.
"""

from __future__ import annotations

from pathlib import Path

UCD_VERSION = "16.0.0"
ROOT = Path(__file__).resolve().parents[1]
UCD_DIR = ROOT / "third_party" / "ucd" / UCD_VERSION
OUT = ROOT / "app" / "schemas" / "unicode_props.py"

# UTS #51: Regional_Indicator es el bloque fijo U+1F1E6..U+1F1FF.
REGIONAL_INDICATOR = ((0x1F1E6, 0x1F1FF),)


def _parse_code(token: str) -> int:
    return int(token, 16)


def parse_ucd_property(path: Path, property_name: str) -> tuple[tuple[int, int], ...]:
    ranges: list[tuple[int, int]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("@"):
            continue
        codes, _, prop = line.partition(";")
        if prop.strip().split()[0] != property_name:
            continue
        token = codes.strip()
        if ".." in token:
            start_s, end_s = token.split("..", 1)
            ranges.append((_parse_code(start_s), _parse_code(end_s)))
        else:
            code = _parse_code(token)
            ranges.append((code, code))
    return _merge(ranges)


def _merge(ranges: list[tuple[int, int]]) -> tuple[tuple[int, int], ...]:
    if not ranges:
        return ()
    ordered = sorted(ranges)
    merged: list[tuple[int, int]] = [ordered[0]]
    for start, end in ordered[1:]:
        prev_start, prev_end = merged[-1]
        if start <= prev_end + 1:
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))
    return tuple(merged)


def parse_zwj_sequences(path: Path) -> tuple[str, ...]:
    """Secuencias RGI_Emoji_ZWJ_Sequence de emoji-zwj-sequences.txt."""

    sequences: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        codes, _, rest = line.partition(";")
        prop, _, _tail = rest.partition(";")
        if prop.strip() != "RGI_Emoji_ZWJ_Sequence":
            continue
        sequences.append("".join(chr(int(token, 16)) for token in codes.split()))
    return tuple(sequences)


def parse_assigned(path: Path) -> tuple[tuple[int, int], ...]:
    """Rangos asignados (General_Category distinta de Cn) en Unicode 16.0.0."""

    ranges: list[tuple[int, int]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("@"):
            continue
        codes, _, prop = line.partition(";")
        if prop.strip().split()[0] == "Cn":
            continue
        token = codes.strip()
        if ".." in token:
            start_s, end_s = token.split("..", 1)
            ranges.append((_parse_code(start_s), _parse_code(end_s)))
        else:
            code = _parse_code(token)
            ranges.append((code, code))
    return _merge(ranges)


def parse_tables(ucd_dir: Path) -> dict[str, tuple[tuple[int, int], ...]]:
    joining = ucd_dir / "DerivedJoiningType.txt"
    emoji = ucd_dir / "emoji-data.txt"
    category = ucd_dir / "DerivedGeneralCategory.txt"
    return {
        "JOINING_L": parse_ucd_property(joining, "L"),
        "JOINING_D": parse_ucd_property(joining, "D"),
        "JOINING_R": parse_ucd_property(joining, "R"),
        "JOINING_T": parse_ucd_property(joining, "T"),
        "EXTENDED_PICTOGRAPHIC": parse_ucd_property(emoji, "Extended_Pictographic"),
        "EMOJI_PRESENTATION": parse_ucd_property(emoji, "Emoji_Presentation"),
        "EMOJI_MODIFIER_BASE": parse_ucd_property(emoji, "Emoji_Modifier_Base"),
        "REGIONAL_INDICATOR": REGIONAL_INDICATOR,
        "ASSIGNED": parse_assigned(category),
    }


def _format_ranges(ranges: tuple[tuple[int, int], ...]) -> str:
    if len(ranges) == 1:
        start, end = ranges[0]
        return f"((0x{start:04X}, 0x{end:04X}),)"
    parts = [f"(0x{start:04X}, 0x{end:04X})" for start, end in ranges]
    inner = ",\n    ".join(parts)
    return f"(\n    {inner},\n)"


def render(tables: dict[str, tuple[tuple[int, int], ...]]) -> str:
    chunks = [
        '"""Tablas UCD congeladas. Generado; no editar a mano.',
        "",
        f"Fuente: Unicode {UCD_VERSION}.",
        "DerivedJoiningType.txt, emoji-data.txt, DerivedGeneralCategory.txt.",
        '"""',
        "",
        f'UCD_VERSION = "{UCD_VERSION}"',
        "",
    ]
    for name, ranges in tables.items():
        chunks.append(f"{name}: tuple[tuple[int, int], ...] = {_format_ranges(ranges)}")
        chunks.append("")
    return "\n".join(chunks)


def main() -> None:
    tables = parse_tables(UCD_DIR)
    OUT.write_text(render(tables), encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
