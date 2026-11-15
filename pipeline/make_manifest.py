# make_manifest.py —— 由题图目录生成 A 阶段 manifest
# 用法: python make_manifest.py --dir evidence --out manifest.json --session 1.2 [--meta quiz.json]
import argparse, json, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import load_json, save_json

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--session", default="")
    ap.add_argument("--meta", default=None, help="可选 quiz.json（含 type 字段）")
    args = ap.parse_args()

    types = {}
    if args.meta and os.path.exists(args.meta):
        m = load_json(args.meta)
        for q in m.get("questions", []):
            types[q.get("index")] = q.get("type")

    items = []
    for name in os.listdir(args.dir):
        mm = re.match(r"^q(\d+)\.png$", name)
        if not mm:
            continue
        idx = int(mm.group(1))
        items.append({"index": idx, "image": os.path.join(args.dir, name), "dom_type": types.get(idx)})
    items.sort(key=lambda x: x["index"])
    if not items:
        print("ERROR: no q*.png found in " + args.dir, flush=True); return 2
    out = {"session": args.session, "questions": items}
    save_json(args.out, out)
    print("manifest written: %s (%d questions)" % (args.out, len(items)), flush=True)
    for it in items:
        print("  Q%d %s dom_type=%s" % (it["index"], it["image"], it["dom_type"]), flush=True)
    return 0

if __name__ == "__main__":
    sys.exit(main())
