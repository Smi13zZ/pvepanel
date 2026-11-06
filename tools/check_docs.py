#!/usr/bin/env python3
"""check_docs.py - every image and local link in the docs must resolve.

A relative image path in Markdown is resolved against the file that contains
it, not the repository root. Writing `![](foo.png)` inside `docs/guide.md`
therefore points at `docs/foo.png` - and GitHub answers 404 while the file is
sitting right there in `docs/images/`. That is invisible in a local editor
preview, so it only shows up when someone opens the page on GitHub.

Runs in CI and in tools/preflight.py so a broken path fails before release.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# (markdown file, directory its relative paths resolve against)
DOCS = [
    ("README.md", ROOT),
    ("README_EN.md", ROOT),
    ("CHANGELOG.md", ROOT),
]
DOCS_DIR = os.path.join(ROOT, "docs")
if os.path.isdir(DOCS_DIR):
    for name in sorted(os.listdir(DOCS_DIR)):
        if name.endswith(".md"):
            DOCS.append((os.path.join("docs", name), DOCS_DIR))

IMG_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
LINK_RE = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")

problems = []
checked = 0

for doc, basedir in DOCS:
    path = os.path.join(ROOT, doc)
    if not os.path.isfile(path):
        problems.append("%s: file listed but missing" % doc)
        continue
    text = open(path, "r", encoding="utf-8").read()
    for regex, kind in ((IMG_RE, "image"), (LINK_RE, "link")):
        for m in regex.finditer(text):
            ref = m.group(1).split()[0].strip()
            if not ref or ref.startswith(("http://", "https://", "mailto:", "#")):
                continue
            target = ref.split("#")[0]
            if not target:
                continue
            resolved = os.path.normpath(os.path.join(basedir, target))
            # Links that climb out of the repository (e.g. ../../releases) are
            # GitHub navigation, not files - they cannot be checked from a clone.
            if not os.path.abspath(resolved).startswith(os.path.abspath(ROOT) + os.sep):
                continue
            checked += 1
            if not os.path.exists(resolved):
                line = text[: m.start()].count("\n") + 1
                problems.append("%s:%d  %s -> %s (resolves to %s)"
                                % (doc, line, kind, ref, resolved))

print("checked %d local references in %d documents" % (checked, len(DOCS)))
for p in problems:
    print("  BROKEN " + p)
print("")
print("problems: %d" % len(problems))
print("RESULT: " + ("PASS" if not problems else "FAIL"))
sys.exit(1 if problems else 0)
