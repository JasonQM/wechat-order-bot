import sys
import time

sys.path.insert(0, "src")
import window as W
import ocr_engine
import perceive

W.init_dpi()
win = W.pick_main_window()
if not win:
    print("没找到微信窗口")
    raise SystemExit(1)

hwnd = win["hwnd"]
img, _, _ = W.capture(hwnd)
lay = W.compute_layout(*img.size)
print("窗口", img.size)
print()

for mw in (None, 2200, 2000, 1800, 1400):
    t = time.time()
    lines = ocr_engine.ocr(img, max_width=mw)
    el = time.time() - t
    title = perceive.read_header(lines, lay)
    flag = "有字" if len(lines) > 10 else "太少"
    print(f"  max_width={str(mw):6s}  {el:5.2f}s  {len(lines):3d} 行  [{flag}]  顶栏={title!r}")
