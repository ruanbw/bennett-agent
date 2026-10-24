#!/usr/bin/env python3
"""把 `record.mjs` 录出来的原始 webm 剪成 README 里用的三件东西。

为什么需要剪辑而不是直接转码：录屏是**真实时间**——模型想 6 秒、首次建机器再等几秒，
原片里一半是同屏不动的等待。原速发布没人看完，均匀 2× 又会让打字和流式输出变形。
所以按 `timeline.json` 里的事件切段，逐段设速度：

    blank     页加载 + 登录        ——     整段剪掉（不是这期演示的主题）
    ready     空态停留            1.0×   让人看清这是什么产品、能问什么
    typing    键入提问            1.3×   打字是「有人在用」的信号，但不必看满 5 秒
    submitted 等模型 / 开机       4.0×   同屏不动，加速到几乎不留
    tool      执行 + 流式回答     1.0×   正片，必须原速
    answered  展开工具轨迹        1.0×   证据画面（脚本路径、退出码、每步耗时）
    expanded  结尾停留            1.0×

产出：

    demo.mp4    1440×900 H.264，静音，README 里链过去的完整版
    demo.gif    1000 宽的短循环，README 里内联自动播放（GitHub 不支持 <video>）
    cover.png   MP4 链接的封面（工具轨迹展开后的那一帧）

用法：

    uv run python scripts/demo/make-video.py                    # 默认读 docs/demo
    uv run python scripts/demo/make-video.py --speed tool=1.5   # 调某一段速度
    uv run python scripts/demo/make-video.py --cut blank        # 整段丢掉
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

#: 每段默认速度。键是 `timeline.json` 里的 mark 名，值是「这个 mark 到下个 mark」的速度。
DEFAULT_SPEEDS: dict[str, float] = {
    "ready": 1.0,
    "typing": 1.3,
    "submitted": 4.0,
    "tool": 1.0,
    "answered": 1.0,
    "expanded": 1.0,
}

#: 默认整段剪掉的 span：加载与登录属于「怎么进来」，不属于「它能做什么」。
DEFAULT_CUT: set[str] = {"blank"}

#: GIF 的取景：从「刚发出」到结尾。前面空态与打字的 5 秒不值得让 GIF 变大一倍。
GIF_FROM = "submitted"
GIF_SPEED = 1.5
#: GIF 内部再按段给一次速度：等待那段（GIF 里占时最多、信息量最少）抽得比其他段更狠，
#: 而回答和证据画面只是略快。GIF 的单位成本是「体积」，不是「时长」。
GIF_SPEEDS: dict[str, float] = {
    "submitted": 6.0,
    "tool": 1.2,
    "answered": 1.0,
    "expanded": 1.0,
}
GIF_WIDTH = 900
GIF_FPS = 10
GIF_COLORS = 160

MP4_WIDTH = 1440
MP4_FPS = 25


def run(cmd: list[str]) -> None:
    """跑一条 ffmpeg，失败时把 stderr 尾巴打出来（ffmpeg 的报错很长，全打会淹掉上下文）。"""
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        sys.stderr.write("".join(result.stderr.splitlines(keepends=True)[-25:]))
        raise SystemExit(f"ffmpeg 失败：{' '.join(cmd[:6])} …")


def probe_duration(path: Path) -> float:
    """读视频真实时长（秒）。"""
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(result.stdout.strip())


def spans_from_marks(
    marks: list[dict], speeds: dict[str, float], cut: set[str], *, scale: float = 1.0
) -> list[tuple[str, float, float, float]]:
    """把 mark 序列变成 (name, 起, 止, 速度) 列表（单位秒）。

    `scale` 是**墙钟 → 视频时间**的修正系数：mark 是脚本按 `Date.now()` 打的，
    而 Playwright 录出来的 webm 会有 5% 左右的时间膨胀（编码器排队，与实时无关）。
    不修正的话越到后面偏得越多——1 分钟的片子末尾能差 2 秒，剪辑点就会切错画面
    （比如「展开工具轨迹」那一段会拍到展开前的样子）。系数由真实时长反推。
    """
    spans: list[tuple[str, float, float, float]] = []
    for index, mark in enumerate(marks[:-1]):
        name = mark["name"]
        start = mark["at"] / 1000 * scale
        end = marks[index + 1]["at"] / 1000 * scale
        if end - start < 0.05:
            continue  # 同一拍打的两个 mark，切出来只会是空片段
        if name in cut:
            continue
        spans.append((name, start, end, max(speeds.get(name, 1.0), 0.1)))
    return spans


def trim_chain(spans: list[tuple[str, float, float, float]], *, width: int, fps: int) -> str:
    """生成 `trim,setpts,scale,concat` 的 filter graph：每段独立变速后再拼起来。"""
    parts: list[str] = []
    for index, (_name, start, end, speed) in enumerate(spans):
        # 先 trim 再 setpts：顺序反了会把时间戳改成负值，ffmpeg 会静默丢帧
        parts.append(
            f"[0:v]trim=start={start:.3f}:end={end:.3f},setpts=(PTS-STARTPTS)/{speed},"
            f"scale={width}:-2:flags=lanczos,fps={fps},format=yuv420p[v{index}]"
        )
    labels = "".join(f"[v{index}]" for index in range(len(spans)))
    parts.append(f"{labels}concat=n={len(spans)}:v=1:a=0[out]")
    return ";".join(parts)


def encode_mp4(src: Path, dst: Path, spans) -> None:
    chain = trim_chain(spans, width=MP4_WIDTH, fps=MP4_FPS)
    # 命令行参数按「一组一个」排更接近它跑起来的样子；fmt 下面几块保持原样
    # fmt: off
    run(
        [
            "ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
            "-filter_complex", chain, "-map", "[out]",
            "-c:v", "libx264", "-profile:v", "high", "-crf", "24",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an", str(dst),
        ]
    )
    # fmt: on


def encode_gif(src: Path, dst: Path, spans, *, start_mark: str, speed: float) -> None:
    """GIF 两遍法：先 palettegen 再 paletteuse。

    不这么做的话 256 色调色板由 ffmpeg 默认的通用表给出，浅色 UI 上会出现明显的
    色带与麻点（这一屏几乎全是白底 + 灰边）。
    """
    if not any(name == start_mark for name, *_ in spans):
        raise SystemExit(f"GIF 起点 {start_mark!r} 不在时间轴里，可用：{[s[0] for s in spans]}")
    # 从起点那个 mark 开始截取尾段（GIF 只承担「一眼看懂发生了什么」，不承担全过程）
    window = []
    seen = False
    for name, start, end, _speed in spans:
        seen = seen or name == start_mark
        if seen:
            window.append((name, start, end, GIF_SPEEDS.get(name, speed)))

    chain = trim_chain(window, width=GIF_WIDTH, fps=GIF_FPS)
    with tempfile.TemporaryDirectory() as tmp:
        palette = Path(tmp) / "palette.png"
        # fmt: off
        run(
            [
                "ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
                "-filter_complex", chain.replace("[out]", "[vout]")
                + f";[vout]palettegen=max_colors={GIF_COLORS}:stats_mode=diff[pal]",
                "-map", "[pal]", str(palette),
            ]
        )
        # fmt: on
        # fmt: off
        run(
            [
                "ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-i", str(palette),
                "-filter_complex", chain + ";[out][1:v]paletteuse=dither=bayer:bayer_scale=3[gif]",
                "-map", "[gif]", "-loop", "0", str(dst),
            ]
        )
        # fmt: on


def export_poster(src: Path, dst: Path, spans, *, at_mark: str) -> None:
    for name, start, _end, _speed in spans:
        if name == at_mark:
            # fmt: off
            run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{start + 0.6:.3f}", "-i", str(src),
                 "-frames:v", "1", "-vf", f"scale={MP4_WIDTH}:-2", str(dst)])
            # fmt: on
            return
    raise SystemExit(f"封面时间点 {at_mark!r} 不在剪辑后的时间轴里")


def main() -> None:
    parser = argparse.ArgumentParser(description="把录屏剪成 README 用的 MP4 + GIF + 封面")
    parser.add_argument("--dir", default="docs/demo", help="录屏与产物的目录")
    parser.add_argument("--speed", action="append", default=[], metavar="段=倍速", help="覆盖某段速度")
    parser.add_argument("--cut", action="append", default=[], metavar="段", help="整段丢掉（默认已丢 blank）")
    parser.add_argument("--gif-from", default=GIF_FROM, help="GIF 从哪个 mark 开始")
    parser.add_argument("--gif-speed", type=float, default=GIF_SPEED)
    args = parser.parse_args()

    if shutil.which("ffmpeg") is None:
        raise SystemExit("没找到 ffmpeg（brew install ffmpeg）")

    out = Path(args.dir)
    raw = out / "raw.webm"
    timeline_path = out / "timeline.json"
    for required in (raw, timeline_path):
        if not required.exists():
            raise SystemExit(f"缺少 {required}，先跑 node scripts/demo/record.mjs")

    timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
    speeds = dict(DEFAULT_SPEEDS)
    for override in args.speed:
        name, _, value = override.partition("=")
        speeds[name] = float(value)

    video_seconds = probe_duration(raw)
    wall_seconds = timeline["marks"][-1]["at"] / 1000
    scale = video_seconds / wall_seconds if wall_seconds else 1.0
    print(f"时间轴修正 ×{scale:.3f}（墙钟 {wall_seconds:.1f}s → 视频 {video_seconds:.1f}s）")
    spans = spans_from_marks(timeline["marks"], speeds, set(DEFAULT_CUT) | set(args.cut), scale=scale)
    if not spans:
        raise SystemExit("时间轴切出来是空的，检查 --cut 参数")

    total = sum((end - start) / speed for _n, start, end, speed in spans)
    print(f"剪辑后时长 ~{total:.1f}s（原片 {(timeline['marks'][-1]['at'] / 1000):.1f}s）")
    for name, start, end, speed in spans:
        print(f"  {name:10s} {start:6.1f}s → {end:6.1f}s  {speed:>4.1f}×  成片 {(end - start) / speed:5.1f}s")

    encode_mp4(raw, out / "demo.mp4", spans)
    encode_gif(raw, out / "demo.gif", spans, start_mark=args.gif_from, speed=args.gif_speed)
    export_poster(raw, out / "cover.png", spans, at_mark="expanded")

    for name in ("demo.mp4", "demo.gif", "cover.png"):
        path = out / name
        print(f"  {path}  {path.stat().st_size / 1024 / 1024:.2f} MB")


if __name__ == "__main__":
    main()
