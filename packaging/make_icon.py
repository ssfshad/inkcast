"""Draws InkCast's icon (the same red circle and white pen stroke as the tray icon) as a Windows .ico."""
import os
import sys

from PIL import Image, ImageDraw

S = 1024  # draw big, then scale down so the edges are smooth


def icon(size):
    k = S / 64
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.ellipse([6 * k, 6 * k, 58 * k, 58 * k], fill="#ff3b30")
    w = round(6 * k)
    d.line([20 * k, 44 * k, 44 * k, 20 * k], fill="white", width=w)
    for x, y in ((20, 44), (44, 20)):  # round caps
        d.ellipse([x * k - w / 2, y * k - w / 2, x * k + w / 2, y * k + w / 2], fill="white")
    return im.resize((size, size), Image.LANCZOS)


out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "inkcast.ico")
sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
icon(256).save(out, sizes=[(s, s) for s in sizes], append_images=[icon(s) for s in sizes])
print("wrote", out)
