import { useEffect, useMemo, useRef, useState } from 'react';
import { Link, useLocation } from 'react-router-dom';
import { ArrowLeft, BookOpen, Download, RefreshCw } from 'lucide-react';

import HelpContent from '@/components/help/HelpContent';
import HelpSearch from '@/components/help/HelpSearch';
import { Button } from '@/components/ui/button';
import manual from '@/content/manual/manifest.json';
import { buildManualSections } from '@/content/manual/manualSearch';

const chapterSources = import.meta.glob<string>('../content/manual/chapters/*.md', { query: '?raw', import: 'default' });

export default function HelpPage() {
  const location = useLocation();
  const top = useRef<HTMLDivElement>(null);
  const [chapters, setChapters] = useState<string[] | null>(null);
  const [error, setError] = useState(false);
  const sections = useMemo(() => chapters ? buildManualSections(manual.chapters.map((chapter, index) => ({
    id: chapter.id, title: chapter.title, content: chapters[index],
  }))) : [], [chapters]);
  let anchor = '';
  try { anchor = decodeURIComponent(location.hash.slice(1)); } catch { /* Invalid URLs fall back to the beginning. */ }
  const currentChapter = sections.find((section) => section.id === anchor)?.chapterId ?? manual.chapters[0].id;
  const [tocOpen, setTocOpen] = useState(false);
  const [expandedChapters, setExpandedChapters] = useState<Set<string>>(
    () => new Set(manual.chapters.map((chapter) => chapter.id)),
  );

  useEffect(() => {
    let cancelled = false;
    Promise.all(manual.chapters.map((chapter) => {
      const load = chapterSources[`../content/manual/${chapter.source}`];
      if (!load) return Promise.reject(new Error(`Missing manual chapter: ${chapter.id}`));
      return load();
    })).then((contents) => { if (!cancelled) setChapters(contents); })
      .catch(() => { if (!cancelled) setError(true); });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    if (!chapters) return;
    const frame = requestAnimationFrame(() => {
      const target = sections.some((section) => section.id === anchor) ? document.getElementById(anchor) : top.current;
      target?.scrollIntoView({ block: 'start' });
      if (target && target !== top.current && anchor.includes('--section-')) target.focus({ preventScroll: true });
    });
    return () => cancelAnimationFrame(frame);
  }, [chapters, sections, anchor, location.key]);

  useEffect(() => {
    setExpandedChapters((expanded) => {
      if (expanded.has(currentChapter)) return expanded;
      return new Set(expanded).add(currentChapter);
    });
  }, [currentChapter]);

  function navigateFromToc(chapterId: string) {
    setExpandedChapters((expanded) => new Set(expanded).add(chapterId));
    setTocOpen(false);
  }

  function toggleChapter(chapterId: string) {
    setExpandedChapters((expanded) => {
      const next = new Set(expanded);
      if (next.has(chapterId)) next.delete(chapterId);
      else next.add(chapterId);
      return next;
    });
  }

  return (
    <div ref={top} className="min-h-screen bg-[#f7f8fa]">
      <header className="sticky top-0 z-30 border-b border-[#e7ebf2] bg-white">
        <div className="mx-auto grid max-w-[1280px] grid-cols-1 items-center gap-3 px-4 py-4 sm:px-6 md:grid-cols-[minmax(0,1fr)_minmax(300px,340px)] md:gap-x-6 md:gap-y-3 lg:grid-cols-[minmax(0,1fr)_340px_auto] lg:gap-6 lg:px-8 lg:py-5">
          <div className="min-w-0">
            <h1 className="flex items-center gap-2.5 text-[20px] font-semibold leading-7 text-[#24324a] sm:text-[22px] sm:leading-8"><BookOpen className="size-5 shrink-0" aria-hidden="true" />使用帮助</h1>
            <p className="mt-1 text-[13px] leading-5 text-[#5b6478] sm:text-[14px]">Hardware DataBase · {manual.version} · 更新于 {manual.updatedAt}</p>
          </div>
          <HelpSearch sections={sections} disabled={!chapters} />
          <div className="flex flex-wrap items-center justify-start gap-2 sm:justify-end lg:justify-start">
            <Button asChild variant="ghost" className="h-10 gap-1.5 rounded-lg text-[14px] text-[#5b6478]">
              <Link to="/"><ArrowLeft className="size-4" aria-hidden="true" />返回系统</Link>
            </Button>
            <Button asChild variant="outline" className="h-10 gap-2 rounded-lg text-[14px] text-[#24324a]">
              <a href={manual.pdf} download={manual.pdf.split('/').pop()}><Download className="size-4" aria-hidden="true" />下载完整手册（PDF）</a>
            </Button>
          </div>
        </div>
      </header>
      <div className="mx-auto grid max-w-[1280px] grid-cols-1 items-start gap-4 px-4 py-4 sm:gap-6 sm:px-6 sm:py-6 lg:grid-cols-[268px_minmax(0,1fr)] lg:gap-7 lg:px-8 lg:py-7">
        <button
          type="button"
          aria-expanded={tocOpen}
          aria-controls="manual-toc"
          aria-label={tocOpen ? '收起手册目录' : '展开手册目录'}
          onClick={() => setTocOpen((open) => !open)}
          className="flex h-11 items-center justify-between rounded-xl border border-[#e7ebf2] bg-white px-4 text-left text-[14px] font-semibold text-[#24324a] shadow-sm lg:hidden"
        >
          <span className="flex items-center gap-2"><BookOpen className="size-4" aria-hidden="true" />手册目录</span>
          <span className="text-[13px] font-normal text-[#5b6478]">{tocOpen ? '收起' : '展开'}</span>
        </button>
        <nav id="manual-toc" aria-label="手册目录" className={`${tocOpen ? 'block' : 'hidden lg:block'} rounded-2xl border border-[#e7ebf2] bg-white p-3 lg:sticky lg:top-[116px] lg:max-h-[calc(100vh-140px)] lg:overflow-y-auto`}>
          <p className="mb-2 flex items-center gap-2 px-3 py-2 text-[14px] font-semibold text-[#24324a]"><BookOpen className="size-4" aria-hidden="true" />手册目录</p>
          {manual.chapters.map((chapter) => {
            const chapterSections = sections.filter((section) => section.chapterId === chapter.id && section.id !== chapter.id);
            const expanded = expandedChapters.has(chapter.id);
            return (
              <div key={chapter.id}>
                <div className="flex items-center gap-1">
                  <a
                    href={`#${chapter.id}`}
                    aria-current={currentChapter === chapter.id ? 'location' : undefined}
                    onClick={() => navigateFromToc(chapter.id)}
                    className="min-w-0 flex-1 rounded-lg px-3 py-2 text-[14px] leading-[22px] text-[#5b6478] transition-colors hover:bg-[#f3f5f8] hover:text-[#24324a] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring aria-[current=location]:bg-[#edf1f6] aria-[current=location]:font-semibold aria-[current=location]:text-[#24324a]"
                  >{chapter.title}</a>
                  {chapterSections.length > 0 && (
                    <button
                      type="button"
                      aria-label={`${expanded ? '收起' : '展开'} ${chapter.title} 小节`}
                      aria-expanded={expanded}
                      onClick={() => toggleChapter(chapter.id)}
                      className="mr-1 grid size-7 shrink-0 place-items-center rounded-md text-[16px] leading-none text-[#718099] transition-colors hover:bg-[#f3f5f8] hover:text-[#24324a] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                    >
                      <span aria-hidden="true">{expanded ? '−' : '+'}</span>
                    </button>
                  )}
                </div>
                {expanded && chapterSections.length > 0 && (
                  <div className="mb-1 ml-3 border-l border-[#e3e8f0] pl-2">
                    {chapterSections.map((section) => (
                      <a
                        key={section.id}
                        href={`#${section.id}`}
                        aria-current={anchor === section.id ? 'location' : undefined}
                        onClick={() => navigateFromToc(chapter.id)}
                        className="block rounded-md px-2 py-1.5 text-[13px] leading-5 text-[#718099] transition-colors hover:bg-[#f7f8fa] hover:text-[#24324a] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring aria-[current=location]:bg-[#f3f5f8] aria-[current=location]:font-semibold aria-[current=location]:text-[#24324a]"
                      >{section.title}</a>
                    ))}
                  </div>
                )}
              </div>
            );
          })}
        </nav>
        <main aria-label="用户手册正文" className="min-w-0 rounded-2xl border border-[#e7ebf2] bg-white px-4 py-5 sm:px-6 sm:py-6 lg:px-8">
          {error ? (
            <div role="alert" className="py-10 text-center">
              <p className="text-[16px] text-[#5b6478]">手册暂时无法加载，请重新加载页面。</p>
              <Button variant="outline" className="mt-4 gap-2" onClick={() => window.location.reload()}><RefreshCw className="size-4" aria-hidden="true" />重新加载</Button>
            </div>
          ) : chapters ? manual.chapters.map((chapter, index) => (
            <section key={chapter.id} id={chapter.id} aria-labelledby={`${chapter.id}-title`} className="scroll-mt-40 border-b border-border py-7 first:pt-0 last:border-b-0 last:pb-0 lg:scroll-mt-[116px]">
              <h2 id={`${chapter.id}-title`} className="text-[24px] font-semibold leading-8 text-[#24324a] sm:text-[26px]">{chapter.title}</h2>
              <HelpContent content={chapters[index]} sections={sections.filter((section) => section.chapterId === chapter.id)} />
            </section>
          )) : <p role="status" className="py-10 text-center text-[16px] text-[#5b6478]">正在加载使用手册…</p>}
        </main>
      </div>
    </div>
  );
}
