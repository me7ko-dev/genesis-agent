import DOMPurify from 'dompurify';
import hljs from 'highlight.js/lib/common';
import { Marked } from 'marked';

function escape(text: string): string {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

const marked = new Marked({
  gfm: true,
  breaks: true,
  renderer: {
    code({ text, lang }) {
      const language = (lang ?? '').trim().split(/\s+/)[0].toLowerCase();
      let html: string;
      try {
        html = language && hljs.getLanguage(language)
          ? hljs.highlight(text, { language }).value
          : hljs.highlightAuto(text).value;
      } catch {
        html = escape(text);
      }
      const label = escape(language || 'код');
      return `<div class="code-block"><div class="code-head"><span>${label}</span>` +
        `<button class="copy" data-copy>Копирай</button></div>` +
        `<pre><code class="hljs">${html}</code></pre></div>`;
    },
  },
});

/** Agent Markdown → safe HTML (the agent may quote pages and files). */
export function renderMarkdown(text: string): string {
  const raw = marked.parse(text, { async: false }) as string;
  return DOMPurify.sanitize(raw, { ADD_ATTR: ['data-copy', 'target'] });
}
