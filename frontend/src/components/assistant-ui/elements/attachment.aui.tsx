"use client";

import {
  type PropsWithChildren,
  useState,
  type FC,
  isValidElement,
} from "react";
import {
  XIcon,
  PlusIcon,
  FileText,
  Loader2Icon,
  AlertCircleIcon,
} from "lucide-react";
import {
  AttachmentPrimitive,
  ComposerPrimitive,
  useAuiState,
  useAui,
} from "@assistant-ui/react";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import {
  Dialog,
  DialogTitle,
  DialogContent,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Avatar, AvatarImage, AvatarFallback } from "@/components/ui/avatar";
import { TooltipIconButton } from "@/components/assistant-ui/elements/tooltip-icon-button";
import { useAttachmentSrc } from "@/hooks/use-attachment-src";
import { cn } from "@/lib/utils";

type AttachmentPreviewProps = {
  src: string;
  name: string;
};

const AttachmentPreview: FC<AttachmentPreviewProps> = ({ src, name }) => {
  const [isLoaded, setIsLoaded] = useState(false);
  return (
    <img
      src={src}
      alt={`Preview of ${name}`}
      className={cn(
        "block h-auto max-h-[80vh] w-auto max-w-full rounded-sm object-contain transition-opacity duration-300 motion-reduce:transition-none",
        isLoaded
          ? "aui-attachment-preview-image-loaded opacity-100"
          : "aui-attachment-preview-image-loading opacity-0",
      )}
      onLoad={() => setIsLoaded(true)}
    />
  );
};

type AttachmentPreviewDialogProps = PropsWithChildren<{
  src: string | undefined;
  name: string;
}>;

const AttachmentPreviewDialog: FC<AttachmentPreviewDialogProps> = ({
  children,
  src,
  name,
}) => {
  if (!src) return children;

  return (
    <Dialog>
      <DialogTrigger
        nativeButton={false}
        className="aui-attachment-preview-trigger cursor-zoom-in"
        render={
          isValidElement(children) ? (
            children
          ) : (
            <button type="button">{children}</button>
          )
        }
      />
      <DialogContent className="aui-attachment-preview-dialog-content [&>button]:bg-foreground/60 [&>button]:hover:bg-foreground/80 [&_svg]:text-background p-2 sm:max-w-3xl [&>button]:rounded-full [&>button]:p-1 [&>button]:opacity-100 [&>button]:ring-0!">
        <DialogTitle className="aui-sr-only sr-only">
          Preview {name}
        </DialogTitle>
        <div className="aui-attachment-preview bg-background relative mx-auto flex max-h-[80dvh] w-full items-center justify-center overflow-hidden rounded-sm">
          <AttachmentPreview src={src} name={name} />
        </div>
      </DialogContent>
    </Dialog>
  );
};

const AttachmentThumb: FC<{ src: string | undefined }> = ({ src }) => {
  return (
    <Avatar className="aui-attachment-tile-avatar h-full w-full rounded-none after:hidden">
      <AvatarImage
        src={src}
        alt=""
        className="aui-attachment-tile-image rounded-none object-cover"
      />
      <AvatarFallback>
        <FileText className="aui-attachment-tile-fallback-icon text-muted-foreground/80 size-6 stroke-[1.5]" />
      </AvatarFallback>
    </Avatar>
  );
};

const AttachmentUI: FC = () => {
  const aui = useAui();
  const isComposer = aui.attachment.source !== "message";
  const src = useAttachmentSrc();

  const isImage = useAuiState((s) => s.attachment.type === "image");
  const name = useAuiState((s) => s.attachment.name);
  const typeLabel = useAuiState((s) => {
    const type = s.attachment.type;
    switch (type) {
      case "image":
        return "Image";
      case "document":
        return "Document";
      case "file":
        return "File";
      default:
        return type;
    }
  });

  // An attachment on a submission is still being prepared, whether or not the
  // adapter reports progress while it uploads.
  const uploadState = useAuiState((s) =>
    s.attachment.status.type === "incomplete" &&
    s.attachment.status.reason === "error"
      ? "error"
      : s.attachment.status.type === "running" ||
          (s.optional.message?.submission !== undefined &&
            s.attachment.status.type !== "complete")
        ? "uploading"
        : undefined,
  );
  const isUploading = uploadState === "uploading";
  const isError = uploadState === "error";

  const errorMessage = useAuiState((s) =>
    s.attachment.status.type === "incomplete" &&
    s.attachment.status.reason === "error"
      ? (s.attachment.status.message ?? "Upload failed")
      : undefined,
  );

  return (
    <TooltipProvider>
      <Tooltip>
        <AttachmentPrimitive.Root
          className={cn(
            "aui-attachment-root relative",
            isComposer &&
              "animate-in fade-in-0 zoom-in-95 duration-200 motion-reduce:animate-none",
            isImage &&
              !isComposer &&
              "aui-attachment-root-message only:*:first:size-24",
          )}
        >
          <AttachmentPreviewDialog src={src} name={name}>
            <TooltipTrigger
              render={
                <div
                  className={cn(
                    "aui-attachment-tile bg-muted hover:after:bg-foreground/10 focus-visible:ring-ring/50 relative size-14 overflow-hidden rounded-[calc(var(--composer-radius,1rem)-var(--composer-padding,8px))] transition-transform outline-none after:pointer-events-none after:absolute after:inset-0 after:rounded-[inherit] after:ring-1 after:ring-black/10 after:transition-colors after:ring-inset focus-visible:ring-1 motion-reduce:transition-none dark:after:ring-white/10",
                    src
                      ? "cursor-zoom-in active:scale-[0.96]"
                      : "cursor-default",
                    isError &&
                      "after:ring-destructive/60 dark:after:ring-destructive/60",
                  )}
                  role={src ? "button" : "group"}
                  tabIndex={0}
                  onKeyDown={(e) => {
                    if (src && (e.key === "Enter" || e.key === " ")) {
                      e.preventDefault();
                      e.currentTarget.click();
                    }
                  }}
                  aria-label={`${src ? `预览 ${name}` : `${typeLabel} 附件 ${name}`}${
                    isError
                      ? ", 上传失败"
                      : isUploading
                        ? ", 上传中"
                        : ""
                  }`}
                />
              }
            >
              <AttachmentThumb src={src} />
              {isUploading && (
                <div
                  aria-hidden="true"
                  className="aui-attachment-tile-uploading bg-background/60 animate-in fade-in-0 absolute inset-0 flex items-center justify-center backdrop-blur-[2px] motion-reduce:animate-none"
                >
                  <Loader2Icon className="text-muted-foreground size-4 animate-spin" />
                </div>
              )}
              {isError && (
                <div
                  aria-hidden="true"
                  className="aui-attachment-tile-error bg-background/70 animate-in fade-in-0 absolute inset-0 flex items-center justify-center backdrop-blur-[2px] motion-reduce:animate-none"
                >
                  <AlertCircleIcon className="text-destructive size-4" />
                </div>
              )}
            </TooltipTrigger>
          </AttachmentPreviewDialog>
          {isComposer && <AttachmentRemove name={name} />}
        </AttachmentPrimitive.Root>
        <TooltipContent side="top">
          <AttachmentPrimitive.Name />
          {errorMessage && (
            <p className="aui-attachment-error-message">{errorMessage}</p>
          )}
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
};

const AttachmentRemove: FC<{ name: string }> = ({ name }) => {
  return (
    <AttachmentPrimitive.Remove
      render={
        <TooltipIconButton
          tooltip={`Remove ${name}`}
          className="aui-attachment-tile-remove absolute end-1 top-1 size-5 rounded-full bg-black/50! text-white after:absolute after:-inset-1.5 hover:bg-black/70! hover:text-white! active:scale-[0.96] motion-reduce:transition-none"
          side="top"
        />
      }
    >
      <XIcon className="aui-attachment-remove-icon size-3 stroke-[2.5]" />
    </AttachmentPrimitive.Remove>
  );
};

/**
 * 附件暂不支持的显式提示。
 *
 * 这套 UI 曾经能贴图、能预览，但发送时被静默丢弃（`onNew` 只保留 text 分片，
 * 后端 `ChatMessage` 也没有附件字段）。接受输入然后丢掉比没有 UI 更糟。
 *
 * 现在把「不支持」写在用户能看见的地方，并在 Composer 里禁用发送（见 thread.aui.tsx），
 * 使丢弃根本不可能发生——而不是只加一句容易被忽略的提示。
 */
const AttachmentUnsupportedNotice: FC<{ count: number }> = ({ count }) => (
  <div
    role="status"
    className="aui-composer-attachments-notice flex items-start gap-1.5 rounded-md border border-amber-500/30 bg-amber-500/10 px-2 py-1 text-[11px] leading-snug text-amber-700 dark:text-amber-400"
  >
    <AlertCircleIcon className="mt-px size-3 shrink-0" aria-hidden="true" />
    <span>
      暂不支持附件：后端只接受文本，这 {count} 个文件不会被发送。
      点缩略图右上角的 × 移除后即可发送。
    </span>
  </div>
);

/**
 * 输入框上方的附件缩略图 + 不支持提示。
 *
 * 无附件时整体不渲染（此前靠 `empty:hidden` 撑一个空容器）。
 *
 * 注：不再提供 `UserMessageAttachments`（展示已发送消息上的附件）——发送被阻断后
 * 附件永远不会进入消息记录，那个组件是可证明不可达的死代码。
 * 将来后端支持多模态时，需要同时恢复它。
 */
export const ComposerAttachments: FC = () => {
  const count = useAuiState((s) => s.composer.attachments.length);
  if (count === 0) return null;

  return (
    <div className="aui-composer-attachments-wrapper flex w-full flex-col gap-1.5 px-3 pt-2.5">
      <div className="aui-composer-attachments flex w-full flex-row items-center gap-2 overflow-x-auto">
        <ComposerPrimitive.Attachments>
          {() => <AttachmentUI />}
        </ComposerPrimitive.Attachments>
      </div>
      <AttachmentUnsupportedNotice count={count} />
    </div>
  );
};

/**
 * 附件入口：后端只接受文本，因此**明确禁用**而不是打开文件选择器再拦发送。
 *
 * 此前按钮可点、选完文件后才在上方显示「不会发送」的提示：用户已经完成了一次
 * 无效操作。保留按钮（而不是隐藏）是为了让入口位置稳定，并用 aria-label 说明原因；
 * 粘贴/拖拽仍然会经过 `ComposerAttachments` 的提示与发送阻断，作为最后一道保险。
 */
export const ComposerAddAttachment: FC = () => {
  return (
    <TooltipIconButton
      tooltip="附件暂不支持（后端只接受文本）"
      side="bottom"
      variant="ghost"
      size="icon"
      disabled
      className="aui-composer-add-attachment text-muted-foreground hover:text-foreground hover:bg-muted-foreground/15 dark:border-muted-foreground/15 dark:hover:bg-muted-foreground/30 size-7 pointer-coarse:size-8 rounded-full active:scale-[0.96] motion-reduce:transition-none disabled:cursor-not-allowed disabled:opacity-40"
      aria-label="附件暂不支持"
    >
      <PlusIcon className="aui-attachment-add-icon size-4" />
    </TooltipIconButton>
  );
};
