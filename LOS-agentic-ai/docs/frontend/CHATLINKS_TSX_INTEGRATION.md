# ChatLinks.tsx -- integration steps (Vite + React 19 + TypeScript frontend)

Files added (no new dependency -- no react-markdown / remark-gfm):

| File | What it is |
|---|---|
| `frontend/src/runtime/chatbot/api/chatActions.ts` | Typed client: `ContractReply`, `ActionResult` union, `runChatAction(href, {token, chatId, lang})` -> `POST /api/v1/fos/action`, `deliverFile`, link helpers |
| `frontend/src/builders/chatbot/components/ChatLinks.tsx` | `<ChatMarkdown>`: renders the bot's markdown (paragraphs, bold, code, lists, numbered lists, tables, quotes) and makes `ask:` / `action:` links work |

Both pass `npx tsc --noEmit -p tsconfig.app.json` and `npx eslint`. (The project has 3 older tsc errors in
`ProcessPage.tsx`, `AuthContext.tsx`, `speech.ts` -- unused variables, not touched.)

`public/`: the frontend has **no `public/` folder**; `index.html` references `/favicon.svg`, which is therefore a 404
(add `public/favicon.svg` or remove the link). No case data or documents are served from the frontend.

## Step 1 -- read the reply contract (required)

Both chat endpoints now return `{ request_id, markdown, tts }`. `runtime/chatbot/api/client.ts` still reads
`res.answer`, so replies would show "No answer returned". In `formatChatAnswer`:

```ts
const answer = (typeof res.markdown === 'string' ? res.markdown : res.answer || '').trim()
```

and add `markdown?: string; tts?: string` to `ChatQueryResponse`. Use `tts` for read-aloud instead of stripping
the markdown (`utils/speech.ts`).

## Step 2 -- send a chat_id

Add `chat_id` to the body in `queryChat` (and `queryFosCopilot`) -- the conversation id the hook already keeps.
The server remembers the open case / pending question per chat; links like `open_case` need it too.

## Step 3 -- render with ChatMarkdown

In `builders/chatbot/components/MessageBubble.tsx`, for assistant messages replace `renderContent(message.content)`:

```tsx
import { ChatMarkdown } from './ChatLinks'

<ChatMarkdown
  markdown={message.content}
  token={accessToken}               // the same token the chat requests use (useChatbot ctx.accessToken)
  chatId={conversationId}
  lang={replyLanguage}
  onAsk={(text) => onSuggested?.(text)}           // ask: links send the text as the next message
  onReply={(reply) => appendAssistant(reply.markdown)}   // open_case, list_more, confirm_write ...
  onOpenUi={(route) => navigate(route)}           // show_in_ui / show_list_in_ui / new_case
  onUpload={(target) => openUploadPanel(target)}  // POST multipart to target.post_to
  onError={(msg) => showToast(msg)}
  disabled={message.isStreaming}
/>
```

Keep `renderContent` for user messages. `MessageBubble` needs `accessToken`, `conversationId`, `replyLanguage` and an
`appendAssistant` callback passed down from `ChatMessages` / `useChatbot` (all already exist in the hook).

## Step 4 -- check

`npx tsc --noEmit -p tsconfig.app.json && npx eslint src` then in the app: "my cases" -> tap a row's Open,
"download excel" -> the file saves, a case -> Show in UI navigates, a quote -> Copy. 401 / 403 / 404 / 429 show the
server's message through `onError`.
