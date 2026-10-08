// ChatLinks.jsx -- ready to paste. Renders the bot's markdown and makes every link work.
//
//   npm i react-markdown remark-gfm
//
//   <ChatMarkdown markdown={reply.markdown} api={api} onAsk={sendMessage} onOpenUi={navigate} chatId={chatId} lang={lang} />
//
//   api        = { base: "https://<host>", token: "<the user's JWT>" }
//   onAsk      = (text) => void        -- send `text` as the next chat message (ask: links)
//   onOpenUi   = (route) => void       -- open a screen of YOUR app, e.g. "/cases/CASE-852C"
//   onReply    = (reply) => void       -- a chat reply {request_id, markdown, tts} to append (open_case, confirm ...)
//   onUpload   = ({document_type, party, post_to}) => void   -- open a file picker, then POST multipart to post_to
//
// The server checks the user's access on every action; the frontend never decides scope.
import React from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

async function runAction(href, { api, chatId, lang, onReply, onOpenUi, onUpload, onError }) {
  const r = await fetch(`${api.base}/api/v1/fos/action`, {
    method: "POST",
    headers: { "Authorization": `Bearer ${api.token}`, "Content-Type": "application/json" },
    body: JSON.stringify({ href, chat_id: chatId, reply_language: lang }),
  });
  const type = r.headers.get("content-type") || "";
  if (!r.ok) {                                            // 401 / 403 / 404 / 409 / 422 / 429: show the message
    const body = type.includes("json") ? await r.json() : {};
    return onError?.((body.detail && body.detail.message) || `Error ${r.status}`, r.status);
  }
  if (!type.includes("application/json")) {               // a FILE: download (or view) it with the server's name
    const blob = await r.blob();
    const cd = r.headers.get("content-disposition") || "";
    const name = (cd.match(/filename="?([^";]+)"?/) || [])[1] || "download";
    const url = URL.createObjectURL(blob);
    if (cd.startsWith("inline")) { window.open(url, "_blank", "noopener"); return; }   // view_document
    const a = Object.assign(document.createElement("a"), { href: url, download: name });
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
    return;
  }
  const data = await r.json();
  if (data.type === "open_ui") return onOpenUi?.(data.route);      // show_in_ui, show_list_in_ui, new_case
  if (data.type === "upload") return onUpload?.(data);            // the file picker
  if (data.type === "copy") return data;                          // handled by the component (needs the message)
  if (data.type === "reply") return onReply?.(data);              // open_case, list_more, confirm_write ...
}

export function ChatMarkdown({ markdown, api, chatId, lang, onAsk, onReply, onOpenUi, onUpload, onError }) {
  const ref = React.useRef(null);
  const handle = async (href) => {
    if (href.startsWith("ask:")) return onAsk(decodeURIComponent(href.slice(4)));    // send the text as a message
    if (href.startsWith("action:copy")) {                                             // copy the Nth quote, locally
      const n = parseInt(((href.split("ref=")[1] || "draft-1").split("-")[1]) || "1", 10);
      const quote = ref.current?.querySelectorAll("blockquote")[n - 1];
      if (quote) await navigator.clipboard.writeText(quote.innerText);
      return;
    }
    if (href.startsWith("action:")) return runAction(href, { api, chatId, lang, onReply, onOpenUi, onUpload, onError });
  };
  return (
    <div ref={ref} className="bot-markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        // keep action: / ask: hrefs (react-markdown strips unknown protocols by default)
        urlTransform={(url) => url}
        components={{
          a: ({ href = "", children }) =>
            href.startsWith("action:") || href.startsWith("ask:") ? (
              <button type="button" className={`chip ${href.startsWith("action:") ? "chip-action" : "chip-ask"}`}
                      onClick={() => handle(href)}>{children}</button>
            ) : (
              <a href={href} target="_blank" rel="noopener noreferrer">{children}</a>      // normal links: new tab
            ),
        }}
      >
        {markdown}
      </ReactMarkdown>
    </div>
  );
}

export default ChatMarkdown;
