import { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Search, X } from 'lucide-react';

import { searchManual, type ManualSection } from '@/content/manual/manualSearch';

function SearchExcerpt({ text, query }: { text: string; query: string }) {
  const tokens = query.normalize('NFKC').trim().split(/\s+/).filter(Boolean)
    .sort((a, b) => b.length - a.length).map((token) => token.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
  if (!tokens.length) return <>{text}</>;
  const parts = text.split(new RegExp(`(${tokens.join('|')})`, 'gi'));
  return <>{parts.map((part, index) => index % 2 ? <mark key={index} className="rounded-sm bg-amber-100 text-inherit">{part}</mark> : part)}</>;
}

export default function HelpSearch({ sections, disabled }: { sections: ManualSection[]; disabled: boolean }) {
  const navigate = useNavigate();
  const root = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const [query, setQuery] = useState('');
  const [open, setOpen] = useState(false);
  const results = useMemo(() => searchManual(sections, query), [sections, query]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault();
        input.current?.focus();
        setOpen(true);
      }
    };
    const onPointerDown = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener('keydown', onKeyDown);
    document.addEventListener('pointerdown', onPointerDown);
    return () => {
      document.removeEventListener('keydown', onKeyDown);
      document.removeEventListener('pointerdown', onPointerDown);
    };
  }, []);

  return (
    <div ref={root} className="relative min-w-0">
      <div className="flex h-11 items-center gap-2 rounded-xl border border-[#dfe5ee] bg-[#f7f8fa] px-3 focus-within:border-[#94a3b8] focus-within:ring-2 focus-within:ring-[#e2e8f0]">
        <Search className="size-4 shrink-0 text-[#5b6478]" aria-hidden="true" />
        <input
          ref={input}
          type="search"
          aria-label="搜索使用手册"
          aria-controls={open && query.trim() ? 'manual-search-results' : undefined}
          placeholder="搜索手册，如密码、附件、导出"
          disabled={disabled}
          value={query}
          onFocus={() => setOpen(true)}
          onChange={(event) => { setQuery(event.target.value); setOpen(true); }}
          onKeyDown={(event) => {
            if (event.key === 'Escape') { event.preventDefault(); setOpen(false); }
            if (event.key === 'Enter' && results[0]) {
              event.preventDefault();
              navigate(`#${results[0].section.id}`);
              setOpen(false);
            }
          }}
          className="min-w-0 flex-1 bg-transparent text-[14px] text-[#24324a] outline-none placeholder:text-[#5b6478] disabled:opacity-60 [&::-webkit-search-cancel-button]:hidden"
        />
        {query ? (
          <button type="button" aria-label="清除搜索" onClick={() => { setQuery(''); setOpen(false); input.current?.focus(); }} className="rounded p-1 text-[#5b6478] hover:bg-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"><X className="size-4" /></button>
        ) : <kbd className="shrink-0 rounded border border-[#dfe5ee] bg-white px-1.5 py-0.5 text-[11px] text-[#5b6478]">Ctrl K</kbd>}
      </div>
      {open && query.trim() && (
        <section id="manual-search-results" aria-label="手册搜索结果" className="absolute top-full right-0 z-40 mt-2 w-[min(480px,calc(100vw-64px))] overflow-hidden rounded-xl border border-[#dfe5ee] bg-white shadow-lg">
          <p aria-live="polite" className="border-b border-border px-4 py-3 text-[13px] text-[#5b6478]">{results.length ? `找到 ${results.length} 处相关内容${results.length > 8 ? '，显示前 8 项' : ''}` : '没有找到相关内容，请尝试按钮名称或其他关键词，也可浏览左侧目录。'}</p>
          <div className="max-h-[50vh] overflow-y-auto">
            {results.slice(0, 8).map(({ section, snippet }) => (
              <a key={section.id} href={`#${section.id}`} onClick={() => setOpen(false)} className="block border-b border-border px-4 py-3 last:border-b-0 hover:bg-[#f7f8fa] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring">
                <p className="text-[14px] font-semibold text-[#24324a]"><SearchExcerpt text={section.title} query={query} /></p>
                {section.title !== section.chapterTitle && <p className="mt-0.5 text-[12px] text-[#5b6478]">{section.chapterTitle}</p>}
                {snippet && <p className="mt-1 text-[13px] leading-5 text-[#464c5e]"><SearchExcerpt text={snippet} query={query} /></p>}
              </a>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}
