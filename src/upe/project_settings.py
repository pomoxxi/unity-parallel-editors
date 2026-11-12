"""Three-way merges of the shared lists in ProjectSettings, part of merge layer 3.

- `TagManager.asset` `tags`: a set. Both sides' new tags are kept; a tag one
  side removed and the other left alone is removed.
- `TagManager.asset` `layers`: 32 numbered slots. Prefabs and scenes store
  layers by number, so a slot never moves. A slot changed on one side takes
  that change; the same slot changed differently on both sides is a
  conflict (two features both took the first free layer).
- `EditorBuildSettings.asset` `m_Scenes`: keyed by scene GUID. The target's
  order is kept and the branch's new scenes go after it, because the order is
  the build index.

Anything else in the file must merge trivially (one side unchanged), or the
whole file is a conflict.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_SECTION = re.compile(r"^  (\w+):(.*)$")


class SettingsConflict(Exception):
    pass


@dataclass
class _Parsed:
    lines: list[str]
    sections: dict[str, tuple[int, int]]  # key -> (start, end) line span including the header


def _parse(text: str) -> _Parsed:
    lines = text.splitlines()
    sections: dict[str, tuple[int, int]] = {}
    i = 0
    while i < len(lines):
        match = _SECTION.match(lines[i])
        if not match:
            i += 1
            continue
        end = i + 1
        while end < len(lines) and (lines[end].startswith("  - ") or lines[end] == "  -" or lines[end].startswith("    ")):
            end += 1
        sections[match.group(1)] = (i, end)
        i = end
    return _Parsed(lines, sections)


def _items(p: _Parsed, key: str) -> list[list[str]]:
    """A section's list items, each as its lines."""
    if key not in p.sections:
        return []
    start, end = p.sections[key]
    items: list[list[str]] = []
    for line in p.lines[start + 1 : end]:
        if line.startswith("  -"):
            items.append([line])
        elif items:
            items[-1].append(line)
    return items


def _scalar(item: list[str]) -> str:
    return item[0][3:].strip() if len(item[0]) > 3 else ""


def _rest(p: _Parsed, keys: tuple[str, ...]) -> list[str]:
    """The file with the merged sections cut out, for the trivial-merge test."""
    out, i = [], 0
    spans = sorted(p.sections[k] for k in keys if k in p.sections)
    for start, end in spans:
        out += p.lines[i:start] + [p.lines[start].split(":")[0] + ": <merged>"]
        i = end
    return out + p.lines[i:]


def merge_set(base: list[str], ours: list[str], theirs: list[str]) -> list[str]:
    out = [x for x in ours if not (x in base and x not in theirs)]  # drop what the branch removed
    out += [x for x in theirs if x not in base and x not in out]  # add what the branch added
    return out


def merge_slots(base: list[str], ours: list[str], theirs: list[str], what: str) -> list[str]:
    size = max(len(base), len(ours), len(theirs))
    pad = lambda xs: xs + [""] * (size - len(xs))
    b, o, t = pad(base), pad(ours), pad(theirs)
    out, clashes = [], []
    for i in range(size):
        if o[i] == t[i] or t[i] == b[i]:
            out.append(o[i])
        elif o[i] == b[i]:
            out.append(t[i])
        else:
            clashes.append(f"{what} {i}: target has {o[i]!r}, branch has {t[i]!r}")
            out.append(o[i])
    if clashes:
        raise SettingsConflict("; ".join(clashes))
    return out


def merge_keyed(base: list[list[str]], ours: list[list[str]], theirs: list[list[str]], key: str, what: str) -> list[list[str]]:
    def keyed(items):
        out = {}
        for item in items:
            match = re.search(rf"\b{key}: (\S+)", "\n".join(item))
            out[match.group(1) if match else "\n".join(item)] = item
        return out

    b, o, t = keyed(base), keyed(ours), keyed(theirs)
    merged: list[list[str]] = []
    clashes = []
    for k, item in o.items():
        if k in b and k not in t:
            if item != b[k]:
                clashes.append(f"{what} {k}: removed on the branch, changed on the target")
            continue  # removed on the branch
        if k in t and t[k] != item:
            if item == b.get(k):
                item = t[k]
            elif t[k] != b.get(k):
                clashes.append(f"{what} {k}: changed differently on both sides")
        merged.append(item)
    for k, item in t.items():
        if k in o:
            continue
        if k in b:
            if item != b[k]:
                clashes.append(f"{what} {k}: removed on the target, changed on the branch")
            continue  # removed on the target
        merged.append(item)
    if clashes:
        raise SettingsConflict("; ".join(clashes))
    return merged


RULES = {
    "tagmanager.asset": ("tags", "layers"),
    "editorbuildsettings.asset": ("m_Scenes",),
}


def merge_settings(filename: str, base: str, ours: str, theirs: str) -> str:
    """Merge one ProjectSettings file. Raises SettingsConflict with a reason if it cannot."""
    keys = RULES.get(filename.lower())
    if keys is None:
        raise SettingsConflict(f"no merge rule for {filename}")
    pb, po, pt = _parse(base), _parse(ours), _parse(theirs)

    rb, ro, rt = (_rest(p, keys) for p in (pb, po, pt))
    if ro != rt and rt != rb and ro != rb:
        raise SettingsConflict(f"{filename} changed outside {', '.join(keys)} on both sides")
    rest_from = pt if ro == rb and rt != rb else po

    merged: dict[str, list[str]] = {}
    for key in keys:
        bi, oi, ti = _items(pb, key), _items(po, key), _items(pt, key)
        if key == "tags":
            values = merge_set([_scalar(x) for x in bi], [_scalar(x) for x in oi], [_scalar(x) for x in ti])
            merged[key] = [f"  - {v}" for v in values]
        elif key == "layers":
            values = merge_slots([_scalar(x) for x in bi], [_scalar(x) for x in oi], [_scalar(x) for x in ti], "layer")
            merged[key] = [f"  - {v}" if v else "  - " for v in values]
        else:
            items = merge_keyed(bi, oi, ti, "guid", "build scene")
            merged[key] = [line for item in items for line in item]

    out, i = [], 0
    for key, (start, end) in sorted(rest_from.sections.items(), key=lambda kv: kv[1]):
        out += rest_from.lines[i:start]
        if key in merged:
            out.append(f"  {key}:" + ("" if merged[key] else " []"))
            out += merged[key]
        else:
            out += rest_from.lines[start:end]
        i = end
    out += rest_from.lines[i:]
    return "\n".join(out) + "\n"
