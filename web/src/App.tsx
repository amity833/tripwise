import { useCallback, useEffect, useRef, useState } from 'react';
import Markdown from 'react-markdown';
import { api, ApiError, type ChatList, type ConversationView, type Proposal, type Trip } from './api';
import { ChatsPanel } from './ChatsPanel';
import { formatDate, ProposalCard } from './ProposalCard';

type Status = 'sending' | 'queued' | 'sent' | 'failed';
interface Msg { key: string; role: 'user' | 'assistant'; text: string; status?: Status; proposal?: Proposal; quick?: string[] }

const SUGGESTIONS = [
  'I need flights to Berlin from Tel Aviv next month',
  'Business class to London for a client meeting',
  'Show my trips',
];
let counter = 0;
const newId = () => `m-${Date.now().toString(36)}-${(++counter).toString(36)}-${Math.random().toString(36).slice(2, 8)}`;

export function App() {
  const [convId, setConvId] = useState<string>();
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string>();
  /** Conversation ids of the messages still waiting for a reply, one entry per message. */
  const [inflight, setInflight] = useState<string[]>([]);
  const [actionBusy, setActionBusy] = useState(false);
  const [error, setError] = useState<{ text: string; retry: () => void }>();
  const [input, setInput] = useState('');
  const [trips, setTrips] = useState<Trip[]>();
  const [showTrips, setShowTrips] = useState(false);
  const [showChats, setShowChats] = useState(false);
  const [chats, setChats] = useState<ChatList>();
  const [chatsNotice, setChatsNotice] = useState<string>();
  const [switching, setSwitching] = useState(false);
  const bottom = useRef<HTMLDivElement>(null);
  /** The chat on screen. A reply that arrives after switching away must not appear in another chat. */
  const current = useRef<string | undefined>(undefined);

  const hydrate = useCallback(({ conversationId, messages, pendingProposal }: ConversationView) => {
    current.current = conversationId;
    setConvId(conversationId);
    const base: Msg[] = messages.map((m) => ({ key: newId(), role: m.role, text: m.text, status: 'sent' as const }));
    if (pendingProposal) base.push({ key: newId(), role: 'assistant', text: 'You have a pending proposal:', proposal: pendingProposal });
    setMsgs(base);
    setError(undefined);
  }, []);

  const load = useCallback(async () => {
    setLoading(true); setLoadError(undefined);
    try { hydrate(await api.conversation()); }
    catch (e) { setLoadError(e instanceof ApiError ? e.message : 'Could not load your conversation.'); }
    finally { setLoading(false); }
  }, [hydrate]);
  useEffect(() => { void load(); }, [load]);
  useEffect(() => { bottom.current?.scrollIntoView?.({ block: 'end' }); }, [msgs, inflight]);

  const refreshChats = useCallback(async () => {
    try { setChats(await api.conversations()); }
    catch (e) { setError({ text: e instanceof Error ? e.message : 'Could not load your chats.', retry: () => { setError(undefined); void refreshChats(); } }); }
  }, []);
  // Titles and the chat count change when a message is sent or the chat changes.
  useEffect(() => { if (showChats) void refreshChats(); }, [showChats, convId, inflight.length, refreshChats]);

  const patch = (key: string, p: Partial<Msg>) => setMsgs((m) => m.map((x) => (x.key === key ? { ...x, ...p } : x)));

  /** The same messageId is reused on Retry, so the server never runs the message twice. */
  const deliver = useCallback(async (key: string, messageId: string, text: string, cid: string, queued: boolean, seen?: string) => {
    patch(key, { status: queued ? 'queued' : 'sending' });
    setInflight((ids) => [...ids, cid]); setError(undefined);
    try {
      const { reply } = await api.chat(cid, messageId, text, seen);
      patch(key, { status: 'sent' });
      if (current.current !== cid) return; // saved on the server: it shows when that chat is opened again
      setMsgs((m) => {
        // A typed "yes" or "cancel" returns a card that is already on screen: update it there, as the buttons do.
        const p = reply.proposal;
        const onScreen = !!p && m.some((x) => x.proposal?.token === p.token);
        // A new proposal replaces the one before it (a chat has only one live proposal), so older open cards are closed.
        const updated = m.map((x) => {
          const old = x.proposal;
          if (!p || !old) return x;
          if (old.token === p.token) return { ...x, proposal: p };
          const replaced = p.status === 'proposed' && old.status === 'proposed' && old.createdAt < p.createdAt;
          return replaced ? { ...x, proposal: { ...old, status: 'superseded' as const } } : x;
        });
        return [...updated, { key: newId(), role: 'assistant', text: reply.text, proposal: onScreen ? undefined : p, quick: reply.quickReplies }];
      });
    } catch (e) {
      patch(key, { status: 'failed' });
      if (e instanceof ApiError && e.status === 404) {
        setError({ text: 'This chat was deleted.', retry: () => void load() });
        return;
      }
      const ref = e instanceof ApiError && e.requestId ? ` (ref ${e.requestId})` : '';
      const where = current.current === cid ? '' : 'A message in another chat was not delivered. ';
      setError({ text: where + (e instanceof Error ? e.message : 'Something went wrong.') + ref, retry: () => void deliver(key, messageId, text, cid, false, seen) });
    } finally { setInflight((ids) => ids.filter((_, i) => i !== ids.indexOf(cid))); }
  }, [load]);

  const waiting = !!convId && inflight.includes(convId);

  const send = (text: string) => {
    const t = text.trim();
    if (!t || !convId) return;
    const key = newId();
    setMsgs((m) => [...m.map((x) => ({ ...x, quick: undefined })), { key, role: 'user', text: t, status: 'sending' }]);
    setInput('');
    // Tells the server which card is on screen, so a typed "yes" can only approve something the user has seen.
    const seen = [...msgs].reverse().find((m) => m.proposal?.status === 'proposed')?.proposal?.token;
    void deliver(key, newId(), t, convId, waiting, seen);
  };

  const act = async (msgKey: string, token: string, action: 'confirm' | 'cancel' | 'change') => {
    if (!convId) return;
    setActionBusy(true);
    try {
      const out = await api.action(convId, token, action);
      if (action === 'confirm' && out.ok) setTrips(undefined);
      if (current.current !== convId) return;
      if (out.reply.proposal) patch(msgKey, { proposal: out.reply.proposal });
      setMsgs((m) => [...m, { key: newId(), role: 'assistant', text: out.reply.text }]);
    } catch (e) { setError({ text: e instanceof Error ? e.message : 'Action failed.', retry: () => void act(msgKey, token, action) }); }
    finally { setActionBusy(false); }
  };

  /** Runs a request that changes which chat is on screen, one at a time. */
  const changeChat = async (request: () => Promise<ConversationView>, failure: string, closePanel: boolean) => {
    setSwitching(true); setChatsNotice(undefined);
    try {
      hydrate(await request());
      if (closePanel) setShowChats(false); else await refreshChats();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) { // chat limit: show the list so one can be deleted
        setChatsNotice(e.message); setShowChats(true); setShowTrips(false);
      } else {
        setError({ text: e instanceof Error ? e.message : failure, retry: () => void changeChat(request, failure, closePanel) });
      }
    } finally { setSwitching(false); }
  };
  const newChat = () => changeChat(api.newConversation, 'Could not start a new chat.', true);
  const openChat = (id: string) => {
    if (id === convId) { setShowChats(false); return; }
    void changeChat(() => api.openConversation(id), 'Could not open that chat.', true);
  };
  const deleteChat = (id: string) => {
    const viewing = convId;
    // Deleting another chat must leave this one on screen, even if another device has a different current chat.
    const request = async () => {
      const next = await api.deleteConversation(id);
      return viewing && viewing !== id ? api.openConversation(viewing) : next;
    };
    void changeChat(request, 'Could not delete that chat.', false);
  };
  const toggleChats = () => { setShowChats((s) => !s); setShowTrips(false); setChatsNotice(undefined); };
  const toggleTrips = async () => {
    setShowTrips((s) => !s); setShowChats(false);
    if (!showTrips && !trips) { try { setTrips((await api.trips()).trips); } catch { setTrips([]); } }
  };
  const outage = async () => {
    if (!convId) return;
    try { await api.simulateOutage(convId); setMsgs((m) => [...m, { key: newId(), role: 'assistant', text: '_Demo:_ the next message will hit a simulated AI outage (once).' }]); } catch { /* ignore */ }
  };

  const lastQuick = msgs.at(-1)?.quick;
  return (
    <div className="app">
      <header className="top">
        <h1>✈️ Tripwise</h1>
        <nav aria-label="Conversation controls">
          <button onClick={toggleTrips} aria-expanded={showTrips}>My trips</button>
          <button onClick={toggleChats} aria-expanded={showChats}>Chats</button>
          <button onClick={() => void newChat()} disabled={switching}>New chat</button>
        </nav>
      </header>

      {showTrips && (
        <section className="trips" aria-label="My trips">
          {!trips ? <p>Loading…</p> : trips.length === 0 ? <p>No confirmed trips yet.</p> : (
            <ul>{trips.map((t) => (
              <li key={t.reference}>
                <strong>{t.reference}</strong> · ${t.totalUsd}
                {t.flights.map((f) => <div key={f.id}>✈️ {f.origin} → {f.destination} · {formatDate(f.date)} {f.departTime}→{f.arriveTime}</div>)}
                {t.hotel && <div>🏨 {t.hotel.name} · {formatDate(t.hotel.checkIn)} → {formatDate(t.hotel.checkOut)}</div>}
              </li>
            ))}</ul>
          )}
        </section>
      )}

      {showChats && <ChatsPanel list={chats} currentId={convId} busy={switching} notice={chatsNotice} onOpen={openChat} onDelete={deleteChat} />}

      <main className="log" role="log" aria-live="polite" aria-label="Conversation">
        {loading && <div className="skeleton" aria-busy="true" aria-label="Loading conversation"><i /><i /><i /></div>}
        {loadError && <div className="banner" role="alert">{loadError} <button onClick={() => void load()}>Retry</button></div>}
        {!loading && !loadError && msgs.length === 0 && (
          <div className="empty">
            <p>Hi! I plan business trips: flights, hotels, and a policy check. Nothing is booked until you confirm.</p>
            <div className="chips">{SUGGESTIONS.map((s) => <button key={s} onClick={() => send(s)}>{s}</button>)}</div>
          </div>
        )}
        {msgs.map((m) => (
          <div key={m.key} className={`msg ${m.role}`}>
            <div className="bubble">
              {m.role === 'assistant' ? <Markdown skipHtml>{m.text}</Markdown> : <p>{m.text}</p>}
              {m.status && m.status !== 'sent' && <small className={`state ${m.status}`}>{{ sending: 'Sending…', queued: 'Queued…', failed: 'Failed to send' }[m.status]}</small>}
            </div>
            {m.proposal && <ProposalCard p={m.proposal} busy={actionBusy} onAction={(a) => void act(m.key, m.proposal!.token, a)} />}
          </div>
        ))}
        {waiting && <div className="msg assistant"><div className="bubble typing" role="status" aria-label="Assistant is typing"><span /><span /><span /></div></div>}
        <div ref={bottom} />
      </main>

      {error && (
        <div className="banner" role="alert">
          {error.text} <button onClick={error.retry}>Retry</button> <button className="link" onClick={() => setError(undefined)}>Dismiss</button>
        </div>
      )}
      {lastQuick && <div className="chips">{lastQuick.map((q) => <button key={q} onClick={() => send(q)}>{q}</button>)}</div>}

      <form className="composer" onSubmit={(e) => { e.preventDefault(); send(input); }}>
        <label htmlFor="msg" className="sr">Message</label>
        <input id="msg" value={input} onChange={(e) => setInput(e.target.value)} placeholder={msgs.length === 0 ? 'Where are you travelling?' : 'Reply, e.g. "book the second one"'} maxLength={2000} autoComplete="off" disabled={loading || switching || !!loadError} />
        <button className="primary" type="submit" disabled={!input.trim() || loading || switching || !!loadError}>Send</button>
      </form>
      <footer><button className="link" onClick={() => void outage()}>simulate outage</button></footer>
    </div>
  );
}
