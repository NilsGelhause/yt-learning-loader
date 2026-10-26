"""Rebuild deleted teach/ workspace files from kimi session wire logs.

Wire records of type context.append_loop_event carry tool.call events whose
args contain the full file contents the agent wrote. Replay them in order;
the last write to each relative path wins. Paths may be absolute or relative
to the workspace dir.
"""
import json
import sys
from pathlib import Path

SESSIONS_ROOT = Path.home() / ".kimi-code" / "sessions"
TEACH_ROOT = Path(__file__).parent / "teach"


def extract_writes(wire_file: Path, workspace_name: str):
    for line in wire_file.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("type") != "context.append_loop_event":
            continue
        ev = rec.get("event") or {}
        if ev.get("type") != "tool.call":
            continue
        args = ev.get("args") or {}
        name = (ev.get("name") or "").lower()
        if name == "write":
            path, content = args.get("path"), args.get("content")
            if not path or content is None:
                continue
            norm = str(path).replace("\\", "/")
            if workspace_name in norm:
                rel = norm.split(workspace_name, 1)[1].lstrip("/")
            elif not norm.startswith("/") and ":" not in norm.split("/", 1)[0]:
                rel = norm.lstrip("./")
            else:
                continue
            if rel and ".." not in rel:
                yield rel, content, "write"
        elif name in ("edit", "strreplaceedit"):
            path = args.get("path")
            if not path:
                continue
            norm = str(path).replace("\\", "/")
            rel = norm.split(workspace_name, 1)[1].lstrip("/") if workspace_name in norm else norm.lstrip("./")
            if rel and ".." not in rel:
                yield rel, args, "edit"


def rebuild(workspace_name: str):
    hits: dict[str, str] = {}
    for sess_dir in SESSIONS_ROOT.glob(f"wd_{workspace_name}_*"):
        for wire in sess_dir.rglob("wire.jsonl"):
            for rel, payload, kind in extract_writes(wire, workspace_name):
                if kind == "write":
                    hits[rel] = payload
                else:
                    old = hits.get(rel, "")
                    old_s = payload.get("old_string") or payload.get("oldStr") or ""
                    new_s = payload.get("new_string") or payload.get("newStr") or ""
                    if old_s and old_s in old:
                        hits[rel] = old.replace(old_s, new_s, 1)
    if not hits:
        print(f"  {workspace_name}: no writes found")
        return
    for rel, content in sorted(hits.items()):
        dest = TEACH_ROOT / workspace_name / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        print(f"  restored {workspace_name}/{rel} ({len(content)} chars)")


if __name__ == "__main__":
    targets = sys.argv[1:] or [
        "free-i-want-to-learn-ai-prompting",
        "free-teach-me-about-agents",
        "free-i-want-to-learn-about-agents",
    ]
    for t in targets:
        print(f"rebuilding {t}:")
        rebuild(t)
