#!/usr/bin/env python3
"""make_icons.py - build the PVEpanel icon set from the official Proxmox mark.

The mark is taken from the copy Proxmox ships inside pve-manager
(/usr/share/pve-manager/images/logo-128.png). That file is only 128x128, and
naively upscaling it to 512 is visibly soft - which is what the app centre asks
for on high-density screens.

So the source is re-rasterised instead of upscaled:

  1. upscale to 1024 with BICUBIC (smooth, no ringing)
  2. threshold the alpha channel, which snaps the two flat colours back to hard
     edges - the mark is 2-colour artwork, so this recovers the exact original
     geometry rather than inventing it
  3. downsample that to each target size with LANCZOS, which re-introduces
     clean anti-aliasing

That keeps the artwork pixel-faithful to Proxmox's own file while being sharp at
512, and it avoids hand-guessing the arm taper (which I got wrong twice).

The official artwork assumes a light background. On a dark desktop the black
arms would vanish, so a rounded light plate is part of the icon.

Nine sizes are produced because the fnOS app centre swaps {0} in
"images/icon_{0}.png" for whatever density the device reports.
"""
import os
import sys

from PIL import Image

SIZES = (16, 24, 48, 64, 72, 96, 128, 256, 512)

# Where the official artwork comes from. Kept next to this script so the build is
# reproducible on a machine that does not have pve-manager installed.
REF_CANDIDATES = (
    "reference/pve-logo-128.png",
    "tmp/off-128.png",
)

PLATE = (255, 255, 255)
SUPER = 1024


def load_reference(root):
    for rel in REF_CANDIDATES:
        path = os.path.join(root, rel)
        if os.path.isfile(path):
            return Image.open(path).convert("RGBA")
    raise SystemExit(
        "official Proxmox mark not found. Copy it from a PVE host:\n"
        "  scp root@pve:/usr/share/pve-manager/images/logo-128.png "
        "pve-panel/reference/pve-logo-128.png")


def crisp_mark(ref):
    """Re-rasterise the official mark at high resolution with hard edges."""
    big = ref.resize((SUPER, SUPER), Image.BICUBIC)
    alpha = big.getchannel("A")
    # 50% threshold: the source is flat two-colour art, so this restores the
    # original boundary instead of leaving a blurry ramp.
    mask = alpha.point(lambda v: 255 if v >= 128 else 0)
    out = big.copy()
    out.putalpha(mask)
    return out


def draw_mark(ref, size, plate_ratio=0.80, corner_ratio=0.225):
    """One icon: the mark scaled to fit, centred on a rounded light plate."""
    ss = 4
    w = size * ss
    canvas = Image.new("RGBA", (w, w), (0, 0, 0, 0))

    from PIL import ImageDraw
    d = ImageDraw.Draw(canvas)
    d.rounded_rectangle([0, 0, w - 1, w - 1], radius=int(w * corner_ratio), fill=PLATE)

    # the reference has a little transparent padding; crop to its ink first so
    # every size gets the same optical weight
    bbox = ref.getchannel("A").getbbox()
    mark = ref.crop(bbox)

    box = int(w * plate_ratio)
    scale = box / float(max(mark.size))
    new = (max(1, int(round(mark.width * scale))), max(1, int(round(mark.height * scale))))
    mark = mark.resize(new, Image.LANCZOS)
    canvas.paste(mark, ((w - new[0]) // 2, (w - new[1]) // 2), mark)

    return canvas.resize((size, size), Image.LANCZOS)


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ref = load_reference(root)
    mark = crisp_mark(ref)

    ui = os.path.join(root, "src", "fn-pvepanel", "app", "ui", "images")
    web = os.path.join(root, "src", "fn-pvepanel", "app", "web", "images")
    os.makedirs(ui, exist_ok=True)
    os.makedirs(web, exist_ok=True)

    for size in SIZES:
        draw_mark(mark, size).save(os.path.join(ui, "icon_%d.png" % size))
    draw_mark(mark, 512).save(os.path.join(ui, "icon_{0}.png"))
    # ICON.PNG / ICON_256.PNG must stay RGBA. Converting them to RGB flattens
    # the transparent rounded corners onto black, and the app centre then draws
    # four black wedges around the plate - it looks like the icon was never
    # rounded at all. The alpha channel IS the rounding.
    draw_mark(mark, 64).save(os.path.join(root, "src", "fn-pvepanel", "ICON.PNG"))
    draw_mark(mark, 256).save(os.path.join(root, "src", "fn-pvepanel", "ICON_256.PNG"))
    draw_mark(mark, 256).save(os.path.join(web, "icon.png"))

    pad = 14
    total_w = sum(SIZES) + pad * (len(SIZES) + 1)
    sheet = Image.new("RGB", (total_w, 512 + 260 + pad * 4), (38, 38, 41))
    x = pad
    for s in SIZES:
        im = draw_mark(mark, s)
        sheet.paste(im, (x, pad + (512 - s) // 2), im)
        x += s + pad
    zooms = Image.new("RGB", (16 * 8 + 24 * 8 + pad * 3, 24 * 8 + pad * 2), (38, 38, 41))
    zooms.paste(draw_mark(mark, 16).resize((128, 128), Image.NEAREST), (pad, pad))
    zooms.paste(draw_mark(mark, 24).resize((192, 192), Image.NEAREST), (pad * 2 + 128, pad))
    sheet.paste(zooms, (0, 512 + pad * 2))
    sheet.save(os.path.join(root, "image", "icon-sheet.png"))
    draw_mark(mark, 512).save(os.path.join(root, "image", "icon-preview-512.png"))
    draw_mark(mark, 256).save(os.path.join(root, "image", "icon-preview-256.png"))

    a = ref.resize((256, 256), Image.LANCZOS)
    b = draw_mark(mark, 256)
    c = draw_mark(mark, 16).resize((256, 256), Image.NEAREST)
    cmp_img = Image.new("RGB", (768, 256), (120, 120, 124))
    cmp_img.paste(a, (0, 0), a)
    cmp_img.paste(b, (256, 0), b)
    cmp_img.paste(c, (512, 0), c)
    cmp_img.save(os.path.join(root, "image", "icon-compare.png"))

    print("source: official pve-manager logo-128.png, re-rasterised at %dpx" % SUPER)
    print("written: %d sizes + icon_{0} + ICON.PNG + ICON_256.PNG + web icon" % len(SIZES))


if __name__ == "__main__":
    main()