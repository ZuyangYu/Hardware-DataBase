import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type { ManualSection } from '@/content/manual/manualSearch';

import HelpImagePreview from './HelpImagePreview';

export default function HelpContent({ content, sections }: { content: string; sections: ManualSection[] }) {
  return (
    <div className="text-[16px] leading-7 text-[#464c5e] [&_a]:text-primary [&_a]:underline [&_a]:underline-offset-4 [&_blockquote]:my-4 [&_blockquote]:border-l-2 [&_blockquote]:border-border [&_blockquote]:pl-4 [&_blockquote]:text-muted-foreground [&_code]:rounded [&_code]:bg-muted [&_code]:px-1 [&_h3]:mt-6 [&_h3]:mb-2 [&_h3]:text-[18px] [&_h3]:font-semibold [&_h3]:text-foreground [&_li]:my-1 [&_ol]:my-3 [&_ol]:list-decimal [&_ol]:pl-6 [&_p]:my-3 [&_strong]:font-semibold [&_strong]:text-foreground [&_ul]:my-3 [&_ul]:list-disc [&_ul]:pl-6">
      <Markdown
        remarkPlugins={[remarkGfm]}
        skipHtml
        components={{
          h3: ({ node, children }) => <h3 id={sections.find((section) => section.line === node?.position?.start.line)?.id} tabIndex={-1} className="scroll-mt-40 outline-none lg:scroll-mt-[116px]">{children}</h3>,
          img: ({ src, alt }) => src?.startsWith('/help-assets/') ? <HelpImagePreview src={src} alt={alt || '手册截图'} /> : null,
          table: ({ children }) => <div className="my-4 max-w-full overflow-x-auto rounded-lg border border-border"><table className="min-w-[480px] w-full border-collapse text-left text-[14px] sm:min-w-0 [&_td]:min-w-[120px] [&_td]:border-b [&_td]:border-border [&_td]:px-4 [&_td]:py-2 [&_th]:min-w-[120px] [&_th]:border-b [&_th]:border-border [&_th]:bg-muted [&_th]:px-4 [&_th]:py-2 [&_th]:font-medium [&_tr:last-child_td]:border-b-0">{children}</table></div>,
          a: ({ href, children }) => <a href={href} {...(href?.startsWith('http') ? { target: '_blank', rel: 'noopener noreferrer' } : {})}>{children}</a>,
        }}
      >{content}</Markdown>
    </div>
  );
}
