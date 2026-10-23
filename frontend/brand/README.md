# 品牌图标

图标就是头像里的那笔手写连笔字（`avatar-github-460.jpg`，来自 GitHub 账号 ruanbw），
单色使用、不彩色化，形态是**印章**：深底 `#09090b` + 白色墨迹 + 22% 圆角。

选印章而不是透明墨迹的原因：透明墨迹在明暗相反的标签栏上会直接消失
（黑墨迹在深色标签栏、白墨迹在浅色标签栏），要么按主题切两套文件、要么在老浏览器
拿错一套；印章自带底色，两种标签栏都有对比度。

## 产物（`../public/`）

| 文件 | 用途 |
| --- | --- |
| `favicon.svg` | 矢量印章，现代浏览器首选，任意缩放不虚 |
| `favicon.ico` | 16/32/48 多尺寸，兜底老浏览器与 Safari 书签栏 |
| `apple-touch-icon.png` | iOS 添加到主屏；方形满铺，圆角由系统蒙版负责 |
| `safari-pinned-tab.svg` | Safari 固定标签页的单色蒙版，颜色由 Safari 指定 |
| `icons/icon-192.png`、`icons/icon-512.png` | PWA `any` |
| `icons/icon-maskable-512.png` | PWA `maskable`，墨迹收进内切圆安全区 |
| `og.png` | 1200×630 分享图（`og:image` / `twitter:image`） |
| `brand/mark-24@2x.png`、`brand/mark-56@2x.png` | 应用内品牌位（`<BrandMark size={24\|56}>`），48/112 的 2x 光栅 |

应用内的品牌位：登录页标题上方 `size={56}`，侧边栏标题左侧 `size={24}`。
那里**不用** `favicon.svg`：CSS 20–56px 下矢量墨迹只有 0.4–1.0px 粗，会糊成灰雾，
所以按各自的 CSS 尺寸算膨胀量、预先烘成 2x 光栅（`APP_MARK_SPECS`）。
换尺寸要改脚本重生成，不要在调用处用 `className` 缩放图片。

## 重建

```bash
python3 -m venv /tmp/icovenv && /tmp/icovenv/bin/pip install pillow potracer
/tmp/icovenv/bin/python frontend/brand/build-icons.py
```

脚本只依赖 Pillow + potracer + ImageMagick 7，**不进运行时依赖**，也不参与测试。
换母版时替换 `avatar-github-460.jpg` 后重跑即可，其余文件都是派生产物。
把 `python3 -m venv` 换成临时环境是为了不污染项目 venv。

## 三个非显然的细节

- **小尺寸必须加粗，且只能在目标像素空间加粗。** 母版笔画只占画布 2.3%，等比缩到 16px
  只剩 0.33px，会被抗锯齿抹成灰雾。脚本先量出母版笔画宽度（面积/周长），再按各尺寸的
  **目标笔画宽度**（1.35–1.8px）反算膨胀半径；180px 以上笔画本来就够粗，不加工。
  在母版空间膨胀等于整体等比加粗，对缩小后的小图没有任何帮助。
- **SVG 描摹的极性是老坑。** potrace 约定 `0` 为前景，而 PIL 蒙版里墨迹是 `255`，
  忘记取反会去描背景，得到一张「填满整个画布的空白」。
