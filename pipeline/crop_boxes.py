# crop_boxes.py —— 由整视口快照 + bbox 裁剪题图（pyai venv，有 Pillow）
# 用法: python crop_boxes.py --raw capture-raw.json --outdir evidence
import argparse, json, os
from PIL import Image

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--scale", type=float, default=1.6)
    args = ap.parse_args()
    d = json.load(open(args.raw, encoding="utf-8"))
    W = d["viewport"]["w"]; H = d["viewport"]["h"]
    for q in d["questions"]:
        im = Image.open(q["viewport_png"]); sx = im.width / W; sy = im.height / H
        b = q["bbox"]
        x0 = max(int((b["x"] - 4) * sx), 0); y0 = max(int((b["y"] - 4) * sy), 0)
        x1 = min(int((b["x"] + b["width"] + 4) * sx), im.width)
        y1 = min(int((b["y"] + b["height"] + 4) * sy), im.height)
        c = im.crop((x0, y0, x1, y1))
        if args.scale != 1.0:
            c = c.resize((int(c.width * args.scale), int(c.height * args.scale)), Image.LANCZOS)
        p = os.path.join(args.outdir, "q%d.png" % q["index"])
        c.save(p)
        q["image"] = p; q["image_bytes"] = os.path.getsize(p); q["image_size"] = list(c.size)
        print("crop Q%d -> %s %s" % (q["index"], c.size, p), flush=True)
    json.dump(d, open(os.path.join(args.outdir, "quiz.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("crop done: %d questions" % len(d["questions"]), flush=True)

if __name__ == "__main__":
    main()