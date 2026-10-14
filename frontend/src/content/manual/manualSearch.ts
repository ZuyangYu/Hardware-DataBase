export type ManualChapterSource = { id: string; title: string; content: string };
export type ManualSection = { id: string; chapterId: string; chapterTitle: string; title: string; text: string; line: number };
export type ManualSearchResult = { section: ManualSection; snippet: string; score: number };

function plainText(markdown: string): string {
  return markdown
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/<[^>]*>/g, '')
    .replace(/^[|:\-\s]+$/gm, '')
    .replace(/[`*_#|]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

export function buildManualSections(chapters: ManualChapterSource[]): ManualSection[] {
  return chapters.flatMap((chapter) => {
    const sections: ManualSection[] = [];
    let current = { id: chapter.id, chapterId: chapter.id, chapterTitle: chapter.title, title: chapter.title, text: '', line: 0 };
    let body: string[] = [];
    let sectionNumber = 0;
    let fence = '';
    for (const [index, line] of chapter.content.split('\n').entries()) {
      const marker = line.match(/^\s*(`{3,}|~{3,})/);
      if (marker) {
        if (!fence) fence = marker[1][0];
        else if (fence === marker[1][0]) fence = '';
        continue;
      }
      const heading = !fence && line.match(/^###\s+(.+?)\s*#*\s*$/);
      if (heading) {
        sections.push({ ...current, text: plainText(body.join('\n')) });
        current = { ...current, id: `${chapter.id}--section-${++sectionNumber}`, title: plainText(heading[1]), line: index + 1 };
        body = [];
      } else {
        body.push(line);
      }
    }
    sections.push({ ...current, text: plainText(body.join('\n')) });
    return sections;
  });
}

export function searchManual(sections: ManualSection[], query: string): ManualSearchResult[] {
  const normalized = query.normalize('NFKC').trim().toLocaleLowerCase();
  if (!normalized) return [];
  const tokens = [...new Set(normalized.split(/\s+/))];
  return sections.flatMap((section) => {
    const title = section.title.normalize('NFKC').toLocaleLowerCase();
    const chapterTitle = section.chapterTitle.normalize('NFKC').toLocaleLowerCase();
    const text = section.text.normalize('NFKC');
    const body = text.toLocaleLowerCase();
    if (!tokens.every((token) => `${title} ${chapterTitle} ${body}`.includes(token))) return [];
    const score = tokens.reduce((sum, token) => sum + (title.includes(token) ? 20 : chapterTitle.includes(token) ? 8 : 1), title === normalized ? 30 : 0);
    const positions = tokens.map((token) => body.indexOf(token)).filter((index) => index >= 0);
    const start = positions.length ? Math.max(0, Math.min(...positions) - 24) : 0;
    const end = Math.min(text.length, start + 120);
    const snippet = `${start ? '…' : ''}${text.slice(start, end)}${end < text.length ? '…' : ''}`;
    return [{ section, snippet, score }];
  }).sort((a, b) => b.score - a.score);
}
