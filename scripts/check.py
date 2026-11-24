#!/usr/bin/env python3
"""Validate the SDSC marketplace: manifests, plugin versions and skill frontmatter.

    python3 scripts/check.py

Exit code 0 when clean, 1 on any error. Warnings do not fail. Standard library only.
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
SKILL_KEYS = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
LINK_RE = re.compile(r"`((?:references|assets|scripts)/[^`\s]+)`")
errors, warnings = [], []


def frontmatter(text):
    """Top-level keys and the description of a SKILL.md frontmatter block (no YAML library needed)."""
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        return None, ""
    keys = re.findall(r"^([A-Za-z][\w-]*):", m.group(1), re.M)
    desc = re.search(r"^description:\s*(?:>-?|\|-?)?\s*\n?((?:\s+.*\n?)+|.*)", m.group(1), re.M)
    return keys, " ".join((desc.group(1) if desc else "").split())


market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
listed = {p["name"]: p for p in market["plugins"]}
on_disk = {p.name for p in (ROOT / "plugins").iterdir() if p.is_dir()}
for missing in on_disk - set(listed):
    errors.append(f"plugins/{missing} is not listed in marketplace.json")

for name, entry in listed.items():
    pdir = ROOT / entry["source"]
    manifest = pdir / ".claude-plugin" / "plugin.json"
    if not manifest.exists():
        errors.append(f"{name}: no {manifest.relative_to(ROOT)}")
        continue
    plugin = json.loads(manifest.read_text())
    for key in ("name", "version", "description"):
        if plugin.get(key) != entry.get(key):
            errors.append(f"{name}: '{key}' differs between plugin.json and marketplace.json")
    for skill_md in sorted(pdir.glob("skills/*/SKILL.md")):
        sdir = skill_md.parent
        rel = skill_md.relative_to(ROOT)
        text = skill_md.read_text()
        keys, desc = frontmatter(text)
        if keys is None:
            errors.append(f"{rel}: no frontmatter")
            continue
        for k in set(keys) - SKILL_KEYS:
            errors.append(f"{rel}: unknown frontmatter key '{k}'")
        sname = re.search(r"^name:\s*(\S+)", text, re.M)
        if not sname or sname.group(1) != sdir.name or not NAME_RE.match(sdir.name):
            errors.append(f"{rel}: 'name' must equal the folder name and be lower-case-hyphenated")
        if not 20 <= len(desc) <= 1024:
            errors.append(f"{rel}: description is {len(desc)} characters (20-1024)")
        lines = text.count("\n")
        if lines > 300:
            warnings.append(f"{rel}: {lines} lines; move details to references/")
        for ref in set(LINK_RE.findall(text)):
            if not (sdir / ref).exists():
                errors.append(f"{rel}: mentions {ref}, which does not exist")
        if re.search(r"(?i)(token|secret|password)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{20,}", text):
            errors.append(f"{rel}: looks like it contains a credential")

for w in warnings:
    print("warning:", w)
for e in errors:
    print("error:", e)
print(f"{len(listed)} plugin(s), {len(errors)} error(s), {len(warnings)} warning(s)")
sys.exit(1 if errors else 0)
