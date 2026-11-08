"""App icon (drawn, no external art): neon hexagon with a haptic waveform."""
import math
from PIL import Image, ImageDraw, ImageFilter


def make_icon(size=256):
    s = size * 4                                            # supersample, then downscale
    im = Image.new('RGBA', (s, s), (0, 0, 0, 0)); d = ImageDraw.Draw(im)
    c = s / 2; r = s * 0.47
    hexp = [(c + r * math.cos(math.radians(60 * k)), c + r * math.sin(math.radians(60 * k))) for k in range(6)]
    d.polygon(hexp, fill=(6, 16, 30, 255))
    glow = Image.new('RGBA', (s, s), (0, 0, 0, 0)); g = ImageDraw.Draw(glow)
    g.line(hexp + [hexp[0]], fill=(0, 229, 255, 200), width=int(s * 0.06))
    im = Image.alpha_composite(im, glow.filter(ImageFilter.GaussianBlur(s * 0.02)))
    d = ImageDraw.Draw(im)
    d.line(hexp + [hexp[0]], fill=(0, 229, 255, 255), width=int(s * 0.035), joint='curve')
    pts = [(s * 0.22, c), (s * 0.34, c), (s * 0.42, c - s * 0.18), (s * 0.52, c + s * 0.22), (s * 0.61, c - s * 0.10),
           (s * 0.68, c + s * 0.06), (s * 0.78, c)]
    d.line(pts, fill=(0, 229, 255, 255), width=int(s * 0.045), joint='curve')
    return im.resize((size, size), Image.LANCZOS)


if __name__ == '__main__':
    import sys
    make_icon(256).save(sys.argv[1] if len(sys.argv) > 1 else 'release/app.ico',
                        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
