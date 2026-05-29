#!/usr/bin/env python3
"""Check anchor and task queue."""
import json, os, sys

# Check .eite directory
eite_dir = os.path.join(os.path.dirname(__file__), '.eite')
if os.path.isdir(eite_dir):
    print("=== .eite/ contents ===")
    for f in os.listdir(eite_dir):
        fpath = os.path.join(eite_dir, f)
        print(f"  {f} ({os.path.getsize(fpath)} bytes)")
        if f.endswith('.json'):
            try:
                with open(fpath) as fh:
                    print(f"    Content: {json.dumps(json.load(fh), indent=2)[:500]}")
            except:
                pass
else:
    print("No .eite/ directory")

# Check for anchor files
for anchor_path in ['anchor.json', 'ops-anchor.json', '../anchors/ops-anchor.json']:
    full = os.path.join(os.path.dirname(__file__), anchor_path)
    full = os.path.abspath(full)
    if os.path.exists(full):
        print(f"\n=== {anchor_path} ===")
        with open(full) as fh:
            print(fh.read()[:500])

print(f"\n=== Git status ===")
os.chdir(os.path.dirname(__file__))
os.system('git log --oneline -1 2>/dev/null || echo "not a git repo"')
