import { useRef, useState } from 'react';
import { Expand, X } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog';

export default function HelpImagePreview({ src, alt }: { src: string; alt: string }) {
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);

  return (
    <span className="my-5 block">
      <button
        ref={trigger}
        type="button"
        aria-label={`放大图片：${alt}`}
        onClick={() => setOpen(true)}
        className="group relative block w-full cursor-zoom-in overflow-hidden rounded-xl border border-border bg-white text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
      >
        <img src={src} alt={alt} width={1440} height={900} loading="lazy" className="block h-auto w-full" />
        <span className="absolute right-3 bottom-3 inline-flex items-center gap-1.5 rounded-lg border border-border bg-white/95 px-2.5 py-1.5 text-xs text-foreground shadow-sm">
          <Expand className="size-3.5" aria-hidden="true" />放大查看
        </span>
      </button>
      <span className="mt-2 block text-center text-[13px] leading-5 text-muted-foreground">{alt}</span>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent
          showCloseButton={false}
          className="flex max-h-[94vh] max-w-[94vw] flex-col gap-3 p-4 sm:max-w-[94vw]"
          onCloseAutoFocus={(event) => { event.preventDefault(); trigger.current?.focus(); }}
        >
          <div className="flex shrink-0 items-center justify-between gap-4">
            <div>
              <DialogTitle className="text-sm leading-6">{alt}</DialogTitle>
              <DialogDescription className="text-xs">点击关闭或按 Escape 返回手册。</DialogDescription>
            </div>
            <DialogClose asChild>
              <Button variant="outline" size="icon" aria-label="关闭图片预览"><X className="size-4" /></Button>
            </DialogClose>
          </div>
          <div className="min-h-0 overflow-auto rounded-lg border border-border bg-white">
            <img src={src} alt={alt} className="mx-auto block h-auto w-full max-w-none" />
          </div>
        </DialogContent>
      </Dialog>
    </span>
  );
}
