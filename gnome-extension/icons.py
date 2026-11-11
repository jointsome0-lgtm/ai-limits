"""Generates the extension's panel and menu icons, icons/<provider>-symbolic.svg.

Original shapes (not the providers' logos), 16x16, one fill colour and only <path> elements:
GNOME Shell recolours the paths of symbolic icons to the text colour. Run after editing a shape:

    python3 gnome-extension/icons.py
"""
import math
import pathlib
import sys

out = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else \
    pathlib.Path(__file__).resolve().parent / "ai-limits@jointsome0-lgtm.github.io" / "icons"

def f(v): return f"{v:.2f}".rstrip("0").rstrip(".")

def spoke(cx, cy, ang, r2, w0, w1):
    """Tapered ray from the centre (half-width w0) to a round tip (half-width w1); 0 degrees = up."""
    a = math.radians(ang); d = (math.sin(a), -math.cos(a)); n = (math.cos(a), math.sin(a))
    t = (cx + d[0] * (r2 - w1), cy + d[1] * (r2 - w1))
    A = (cx + n[0] * w0, cy + n[1] * w0); B = (t[0] + n[0] * w1, t[1] + n[1] * w1)
    C = (t[0] - n[0] * w1, t[1] - n[1] * w1); D = (cx - n[0] * w0, cy - n[1] * w0)
    return (f"M{f(A[0])} {f(A[1])}L{f(B[0])} {f(B[1])}A{f(w1)} {f(w1)} 0 0 0 {f(C[0])} {f(C[1])}"
            f"L{f(D[0])} {f(D[1])}Z")

def bar(x1, y1, x2, y2, w):
    """Straight stroke with round caps between two points."""
    dx, dy = x2 - x1, y2 - y1; l = math.hypot(dx, dy); n = (-dy / l * w / 2, dx / l * w / 2); h = w / 2
    return (f"M{f(x1 + n[0])} {f(y1 + n[1])}L{f(x2 + n[0])} {f(y2 + n[1])}A{f(h)} {f(h)} 0 0 0 {f(x2 - n[0])} {f(y2 - n[1])}"
            f"L{f(x1 - n[0])} {f(y1 - n[1])}A{f(h)} {f(h)} 0 0 0 {f(x1 + n[0])} {f(y1 + n[1])}Z")

def sparkle(cx, cy, r, k):
    """Four-point star with concave sides; k = how close the sides come to the centre (0..1)."""
    c = r * k
    return (f"M{f(cx)} {f(cy - r)}Q{f(cx + c)} {f(cy - c)} {f(cx + r)} {f(cy)}Q{f(cx + c)} {f(cy + c)} {f(cx)} {f(cy + r)}"
            f"Q{f(cx - c)} {f(cy + c)} {f(cx - r)} {f(cy)}Q{f(cx - c)} {f(cy - c)} {f(cx)} {f(cy - r)}Z")

def svg(title, *paths):
    body = "".join(f'<path d="{p}"/>' for p in paths)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16" fill="#bebebe">'
            f'<title>{title}</title>{body}</svg>\n')

icons = {
    # a spark: eight tapered rays, long on the axes and short on the diagonals
    "claude": svg("Claude", *[spoke(8, 8, a, 7.8, 0.55, 1.25) if a % 90 == 0 else spoke(8, 8, a, 5.0, 0.45, 1.0)
                              for a in range(0, 360, 45)]),
    # a shell prompt: chevron and cursor
    "codex": svg("Codex", bar(3.2, 3.6, 7.6, 8, 2), bar(7.6, 8, 3.2, 12.4, 2), bar(9.6, 12.4, 13.8, 12.4, 2)),
    # twin sparkles (Gemini = the twins)
    "gemini": svg("Gemini", sparkle(6.6, 9.2, 6.2, 0.16), sparkle(12.6, 3.4, 2.8, 0.16)),
}
for name, text in icons.items():
    (out / f"{name}-symbolic.svg").write_text(text)
