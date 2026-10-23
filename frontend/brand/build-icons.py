#!/usr/bin/env python3
"""从 GitHub 头像（`avatar-github-460.jpg`）生成站点全套品牌图标。

品牌设定：头像里那笔手写连笔字就是 Bennett Agent 的标志，单色使用，不彩色化。
图标形态为「印章」：深底（#09090b，与前端深色 --background 同值）+ 白色墨迹，
外加 22% 圆角。这样浅色与深色标签栏上都有对比度，不需要按主题切两套文件
（透明墨迹在相反明度的标签栏上会直接消失）。

产物（相对 frontend/）：
  public/favicon.svg                   矢量印章（现代浏览器首选，缩放不虚）
  public/favicon.ico                   16/32/48 多尺寸印章，兜底老浏览器与 Safari 书签栏
  public/apple-touch-icon.png          180，方形满铺（iOS 自己加圆角蒙版，别提前切角）
  public/safari-pinned-tab.svg         Safari 固定标签页用的单色蒙版（Safari 自己上色）
  public/icons/icon-192.png            PWA any
  public/icons/icon-512.png            PWA any
  public/icons/icon-maskable-512.png   PWA maskable（满铺底 + 墨迹收进安全区）
  public/og.png                        1200×630 社交分享图（绝对路径 /og.png）

用法（不进运行时依赖，只在重建资源时需要）：
  python3 -m venv /tmp/icovenv && /tmp/icovenv/bin/pip install pillow potracer
  /tmp/icovenv/bin/python frontend/brand/build-icons.py

为什么小尺寸要「加粗」：母版在 460px 画布上的笔画只有约 8.4px（占画布 1.8%），
等比缩到 16px 后只剩 0.33px，会被抗锯齿抹成灰雾。所以 16/32/48 三个尺寸在
**目标像素空间**里做一次形态学膨胀（半径 0.42–0.50px），把笔画补到 1.2–1.5px；
180px 以上笔画本来就够粗（3.7px+），保持原样。
膨胀必须发生在目标像素空间（先降采样再膨胀）：在母版空间膨胀等于整体等比加粗，
对缩小后的小图毫无帮助。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover - 只提示，不参与测试
    sys.exit("需要 Pillow：pip install pillow")

BRAND_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = BRAND_DIR.parent
PUBLIC_DIR = FRONTEND_DIR / "public"
PREVIEW_DIR = Path("/tmp/bennett-icons")
SOURCE = BRAND_DIR / "avatar-github-460.jpg"

STAMP = (9, 9, 11)  # 与前端 --background 深色值一致
INK = (255, 255, 255)  # 印章上的墨迹为白
PAPER = (255, 255, 255)
MUTED = (161, 161, 170)

CORNER_RATIO = 0.22  # 圆角占画布比例

# 光栅图标：画布尺寸 → (墨迹留白比例, 目标笔画宽度)；目标为 None 表示不做光学加粗
# 目标值只在 16–48px 生效：再大笑尺寸的笔画本来就够粗，再加工会变成「胖签名」
RASTER_SPECS: dict[int, tuple[float, float | None]] = {
    16: (0.12, 1.35),
    32: (0.12, 1.60),
    48: (0.12, 1.80),
    180: (0.16, None),  # iOS：墨迹略小，四周留白不会被系统蒙版切掉
    192: (0.14, None),
    512: (0.14, None),
}

# 应用内品牌位用的位图（都是 2x 资源）：路径 → (光栅尺寸, CSS 尺寸)
# 不直接用 favicon.svg 的原因：CSS 20–56px 下矢量墨迹只有 0.4–1.0px 粗，
# 会渲染成一层灰雾；这里按 CSS 尺寸算膨胀量、在 2x 光栅上落地。
APP_MARK_SPECS: dict[str, tuple[int, int]] = {
    "brand/mark-24@2x.png": (48, 24),
    "brand/mark-56@2x.png": (112, 56),
}
APP_MARK_PAD = 0.10
APP_MARK_STROKE = 1.45
# 应用内底色用 #18181b 而不是 favicon 的 #09090b：深色主题下恰好等于 --background，
# 印章会整个“消失”只剩墨迹。抬高一级后两种主题下都能看出那块方块的边界。
APP_MARK_STAMP = (24, 24, 27)

SUPERSAMPLE = 8  # 膨胀与圆角都在高分辨率上做再降采样，避免锯齿

MASTER_STROKE = 0.0  # 母版笔画宽度（像素），main() 里量出后填


def log(message: str) -> None:
    print(f"  {message}")


def need(binary: str) -> str:
    path = shutil.which(binary)
    if path is None:
        sys.exit(f"需要 {binary}（ImageMagick 7）")
    return path


MAGICK = need("magick")


def load_mask() -> Image.Image:
    """头像 → 二值墨迹蒙版（墨迹=255），并裁到墨迹包围盒。"""
    gray = Image.open(SOURCE).convert("L")
    mask = gray.point(lambda value: 255 if value < 128 else 0)
    bbox = mask.getbbox()
    if bbox is None:
        sys.exit("头像里没找到墨迹")
    return mask.crop(bbox)


def dilate(mask: Image.Image, radius_px: float) -> Image.Image:
    """圆盘结构元形态学膨胀，半径单位为像素；半径<=0 直接返回。"""
    if radius_px <= 0:
        return mask
    tmp_in = BRAND_DIR / ".dilate-in.png"
    tmp_out = BRAND_DIR / ".dilate-out.png"
    mask.save(tmp_in)
    subprocess.run(
        # 必须拆成两个参数："Dilate Disk:4" 作为单个参数会被 ImageMagick 当成 method 而缺 kernel
        [MAGICK, str(tmp_in), "-morphology", "Dilate", f"Disk:{radius_px}", str(tmp_out)],
        check=True,
        capture_output=True,
    )
    result = Image.open(tmp_out).convert("L").point(lambda v: 255 if v >= 128 else 0)
    tmp_in.unlink(missing_ok=True)
    tmp_out.unlink(missing_ok=True)
    return result


def estimate_stroke(master: Image.Image) -> float:
    """用面积/周长估一下母版里的笔画宽度（笔画细到 1.8% 画布，肉眼估不准）。"""
    from PIL import ImageChops

    area = master.histogram()[255]
    perimeter = 0
    for offset in ((1, 0), (0, 1)):
        diff = ImageChops.difference(master, ImageChops.offset(master, *offset))
        perimeter += diff.histogram()[255]
    return 2 * area / perimeter if perimeter else 0.0


def optical_radius(master: Image.Image, css_size: float, pad: float, target: float | None) -> float:
    """返回 CSS 像素空间的膨胀半径：把该尺寸上的笔画补到 target 像素宽。"""
    if target is None:
        return 0.0
    box = css_size * (1 - 2 * pad)
    native = MASTER_STROKE * box / max(master.size)
    return max(0.0, (target - native) / 2)


def glyph_alpha(master: Image.Image, size: int, pad: float, radius: float) -> Image.Image:
    """把墨迹渲染成 size×size 的 alpha 蒙版（L 模式，墨迹=255，居中，留白按 pad）。

    顺序要紧：超采样画布上放大 → 膨胀（半径按 SUPERSAMPLE 折算）→ 降采样拿平滑边缘。
    """
    hi = size * SUPERSAMPLE
    inner = round(size * (1 - 2 * pad)) * SUPERSAMPLE
    scale = inner / max(master.size)
    resized = master.resize(
        (max(1, round(master.width * scale)), max(1, round(master.height * scale))),
        Image.NEAREST,
    )
    resized = dilate(resized, radius * SUPERSAMPLE)
    canvas = Image.new("L", (hi, hi), 0)
    canvas.paste(resized, ((hi - resized.width) // 2, (hi - resized.height) // 2))
    return canvas.resize((size, size), Image.LANCZOS)


def compose(
    alpha: Image.Image,
    *,
    background: tuple[int, int, int] | None,
    ink: tuple[int, int, int] = INK,
    corner: float = 0.0,
) -> Image.Image:
    """把墨迹蒙版合成为 RGBA 图标；corner>0 时给底切成圆角（外部透明）。"""
    size = alpha.width
    ink_layer = Image.new("RGBA", alpha.size, (*ink, 255))
    if background is None:
        icon = Image.new("RGBA", alpha.size, (0, 0, 0, 0))
    elif corner > 0:
        edge = size * SUPERSAMPLE
        base = Image.new("L", (edge, edge), 0)
        ImageDraw.Draw(base).rounded_rectangle((0, 0, edge - 1, edge - 1), radius=round(corner * edge), fill=255)
        mask = base.resize((size, size), Image.LANCZOS)
        icon = Image.new("RGBA", (size, size), (*background, 255))
        icon.putalpha(mask)
    else:
        icon = Image.new("RGBA", (size, size), (*background, 255))
    icon.paste(ink_layer, (0, 0), alpha)
    return icon


def save_icon(icon: Image.Image, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    icon.save(path, optimize=True)
    log(f"{path.relative_to(FRONTEND_DIR)}  {icon.width}×{icon.height}")


def trace_path(master: Image.Image) -> str | None:
    """墨迹 → SVG path（potracer）。potrace 认 0 为前景，所以要取反。"""
    try:
        import potrace
    except ImportError:
        log("跳过矢量描摹（没装 potracer）")
        return None

    bitmap = potrace.Bitmap(master.point(lambda v: 0 if v >= 128 else 255))
    traced = bitmap.trace(turdsize=2, alphamax=1.0, opttolerance=0.2)

    parts: list[str] = []
    for curve in traced:
        start = curve.start_point
        parts.append(f"M{start.x:.2f} {start.y:.2f}")
        for segment in curve:
            if segment.is_corner:
                parts.append(f"L{segment.c.x:.2f} {segment.c.y:.2f}")
                parts.append(f"L{segment.end_point.x:.2f} {segment.end_point.y:.2f}")
            else:
                parts.append(
                    f"C{segment.c1.x:.2f} {segment.c1.y:.2f} "
                    f"{segment.c2.x:.2f} {segment.c2.y:.2f} "
                    f"{segment.end_point.x:.2f} {segment.end_point.y:.2f}"
                )
        parts.append("Z")
    return "".join(parts)


def build_svg(master: Image.Image, d: str, pad: float = 0.14) -> None:
    """矢量印章：100×100 画布，圆角深底 + 白墨迹。"""
    size = 100.0
    inner = size * (1 - 2 * pad)
    scale = inner / max(master.size)
    tx = (size - master.width * scale) / 2
    ty = (size - master.height * scale) / 2
    stamp = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="100" height="100">\n'
        f'  <rect width="100" height="100" rx="{CORNER_RATIO * 100:.0f}" fill="#09090b"/>\n'
        f'  <g transform="translate({tx:.2f} {ty:.2f}) scale({scale:.4f})">\n'
        f'    <path fill="#ffffff" d="{d}"/>\n'
        f"  </g>\n"
        f"</svg>\n"
    )
    (PUBLIC_DIR / "favicon.svg").write_text(stamp, encoding="utf-8")
    log(f"public/favicon.svg  {len(stamp) / 1024:.1f} KB（矢量印章）")

    # Safari 固定标签页：只给蒙版，Safari 用自己的 color 上色，所以底色必须留空
    mask_svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {master.width} {master.height}" '
        f'width="{master.width}" height="{master.height}">\n'
        f'  <path fill="#000000" d="{d}"/>\n'
        f"</svg>\n"
    )
    (PUBLIC_DIR / "safari-pinned-tab.svg").write_text(mask_svg, encoding="utf-8")
    log(f"public/safari-pinned-tab.svg  {len(mask_svg) / 1024:.1f} KB（单色蒙版）")


def build_ico(master: Image.Image) -> None:
    sizes = (16, 32, 48)
    temps: list[Path] = []
    for size in sizes:
        pad, target = RASTER_SPECS[size]
        icon = compose(
            glyph_alpha(master, size, pad, optical_radius(master, size, pad, target)),
            background=STAMP,
            corner=CORNER_RATIO,
        )
        path = BRAND_DIR / f".ico-{size}.png"
        icon.save(path)
        temps.append(path)
    subprocess.run(
        [MAGICK, *(str(path) for path in temps), str(PUBLIC_DIR / "favicon.ico")],
        check=True,
        capture_output=True,
    )
    for path in temps:
        path.unlink(missing_ok=True)
    log(f"public/favicon.ico  {','.join(map(str, sizes))}")


def load_font(size: int, *, bold: bool = False, cjk: bool = False) -> ImageFont.FreeTypeFont:
    candidates = []
    if cjk:
        # 中文副标题必须是中文字体：Arial / Helvetica 没有 CJK 字形，会渲染成豆腐块
        candidates += [
            "/System/Library/Fonts/Hiragino Sans GB.ttc",
            "/System/Library/Fonts/STHeiti Light.ttc",
        ]
    candidates += [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                continue
    return ImageFont.load_default(size)


def fit_font(
    draw: ImageDraw.ImageDraw,
    text: str,
    max_size: int,
    max_width: float,
    *,
    bold: bool = False,
    cjk: bool = False,
) -> ImageFont.FreeTypeFont:
    """从 max_size 起逐步缩小，直到文本宽度装得进 max_width（中英混排宽度差异很大，不能估）。"""
    size = max_size
    while size > 12:
        font = load_font(size, bold=bold, cjk=cjk)
        if draw.textlength(text, font=font) <= max_width:
            return font
        size -= 2
    return load_font(size, bold=bold, cjk=cjk)


def build_og(master: Image.Image) -> None:
    """1200×630 分享图：深底白墨迹 + 字标，与图标同一套颜色。"""
    width, height = 1200, 630
    margin = 96
    glyph_size = 320
    gap = 64

    image = Image.new("RGB", (width, height), STAMP)
    draw = ImageDraw.Draw(image)

    glyph = glyph_alpha(master, glyph_size, 0.0, 0.0)
    image.paste(Image.new("RGB", glyph.size, INK), (margin, (height - glyph.height) // 2), glyph)

    text_x = margin + glyph_size + gap
    max_text_width = width - margin - text_x
    title = "Bennett Agent"
    subtitle = "PydanticAI + LiteLLM 的 Agent Loop 框架"
    meta = "FastAPI · SSE · React 19"

    title_font = fit_font(draw, title, 92, max_text_width, bold=True)
    subtitle_font = fit_font(draw, subtitle, 34, max_text_width, cjk=True)
    meta_font = fit_font(draw, meta, 28, max_text_width)

    draw.text((text_x, 226), title, font=title_font, fill=INK)
    draw.text((text_x, 344), subtitle, font=subtitle_font, fill=MUTED)
    draw.text((text_x, 402), meta, font=meta_font, fill=(113, 113, 122))

    draw.rectangle([(margin, 96), (width - margin, 99)], fill=(39, 39, 42))
    image.save(PUBLIC_DIR / "og.png", optimize=True)
    log("public/og.png  1200×630")


def build_app_marks(master: Image.Image) -> None:
    """应用内品牌位的位图：按 CSS 尺寸算膨胀量，在 2x 光栅上落地。"""
    for name, (raster, css) in APP_MARK_SPECS.items():
        radius = optical_radius(master, css, APP_MARK_PAD, APP_MARK_STROKE) * (raster / css)
        save_icon(
            compose(
                glyph_alpha(master, raster, APP_MARK_PAD, radius),
                background=APP_MARK_STAMP,
                corner=CORNER_RATIO,
            ),
            PUBLIC_DIR / name,
        )


def build_previews(master: Image.Image) -> None:
    """把小尺寸放大 7 倍并排到 /tmp，便于肉眼核对笔画有没有糊掉。"""
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    cells: list[Image.Image] = []
    for size in (16, 32, 48):
        pad, target = RASTER_SPECS[size]
        icon = compose(
            glyph_alpha(master, size, pad, optical_radius(master, size, pad, target)),
            background=STAMP,
            corner=CORNER_RATIO,
        )
        zoom = icon.resize((size * 7,) * 2, Image.NEAREST)
        for backdrop in (PAPER, (32, 32, 35)):
            sheet = Image.new("RGB", zoom.size, backdrop)
            sheet.paste(zoom, (0, 0), zoom)
            cells.append(sheet)
    gap = 16
    sheet = Image.new(
        "RGB",
        (sum(c.width for c in cells) + gap * (len(cells) + 1), max(c.height for c in cells) + 2 * gap),
        (120, 120, 124),
    )
    x = gap
    for cell in cells:
        sheet.paste(cell, (x, gap))
        x += cell.width + gap
    sheet.save(PREVIEW_DIR / "sizes.png")
    log(f"预览图 → {PREVIEW_DIR}/sizes.png")


def main() -> None:
    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    print("生成品牌图标：")

    master = load_mask()
    master.save(BRAND_DIR / "glyph-master.png", optimize=True)
    log(f"brand/glyph-master.png  {master.width}×{master.height}（裁到墨迹包围盒）")
    global MASTER_STROKE
    MASTER_STROKE = estimate_stroke(master)
    log(f"母版笔画宽度 ≈ {MASTER_STROKE:.2f}px（{100 * MASTER_STROKE / max(master.size):.1f}% 画布）")

    d = trace_path(master)
    if d is not None:
        build_svg(master, d)

    build_ico(master)

    for size, name, background, corner in (
        (180, "apple-touch-icon.png", STAMP, 0.0),  # iOS 自己加圆角，方形满铺
        (192, "icons/icon-192.png", STAMP, CORNER_RATIO),
        (512, "icons/icon-512.png", STAMP, CORNER_RATIO),
        (512, "icons/icon-maskable-512.png", STAMP, 0.0),  # maskable 满铺底
    ):
        pad, target = RASTER_SPECS[size]
        if name.endswith("maskable-512.png"):
            pad = 0.20  # 安全区：墨迹收进内切圆
        save_icon(
            compose(
                glyph_alpha(master, size, pad, optical_radius(master, size, pad, target)),
                background=background,
                corner=corner,
            ),
            PUBLIC_DIR / name,
        )

    build_app_marks(master)
    build_og(master)
    build_previews(master)
    print("完成。")


if __name__ == "__main__":
    main()
