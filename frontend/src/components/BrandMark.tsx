import { cn } from '../lib/utils';

/**
 * 品牌位的印章图标（头像里那笔手写连笔字）。
 *
 * 用位图而不是 `favicon.svg`：CSS 20–56px 下矢量墨迹只有 0.4–1.0px 粗，会糊成
 * 一层灰雾。这两个 2x 资源在生成时就按各自的 CSS 尺寸做了笔画补偿
 * （见 `frontend/brand/build-icons.py` 的 APP_MARK_SPECS），换尺寸请重新生成，
 * 别在调用处用 className 缩放。
 */
const MARKS = {
  24: { src: '/brand/mark-24@2x.png', css: 24 },
  56: { src: '/brand/mark-56@2x.png', css: 56 },
} as const;

export type BrandMarkSize = keyof typeof MARKS;

/**
 * 纯装饰：品牌名由旁边/下面的文字承担，所以 `alt=""` + `aria-hidden`，
 * 避免读屏把「图片」和标题各念一遍。
 */
export function BrandMark({ size = 24, className }: { size?: BrandMarkSize; className?: string }) {
  const mark = MARKS[size];

  return (
    <img
      src={mark.src}
      width={mark.css}
      height={mark.css}
      alt=""
      aria-hidden="true"
      draggable={false}
      className={cn('shrink-0 select-none', className)}
    />
  );
}
