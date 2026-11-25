import { useState } from 'react';
import type { ChatList } from './api';

interface Props {
  list?: ChatList;
  currentId?: string;
  busy: boolean;
  notice?: string;
  onOpen: (id: string) => void;
  onDelete: (id: string) => void;
}

export function ChatsPanel({ list, currentId, busy, notice, onOpen, onDelete }: Props) {
  const [confirming, setConfirming] = useState<string>();
  return (
    <section className="chats" aria-label="Your chats">
      {notice && <p className="notice" role="status">{notice}</p>}
      {!list ? <p>Loading…</p> : (
        <>
          <p className="count">{list.conversations.length} of {list.limit} chats</p>
          <ul>
            {list.conversations.map((c) => (
              <li key={c.id}>
                <button className="open" disabled={busy} aria-current={c.id === currentId ? 'true' : undefined} onClick={() => onOpen(c.id)}>{c.title}</button>
                {confirming === c.id ? (
                  <span className="confirm">
                    <button className="danger" disabled={busy} onClick={() => { setConfirming(undefined); onDelete(c.id); }}>Delete</button>
                    <button onClick={() => setConfirming(undefined)}>Keep</button>
                  </span>
                ) : (
                  <button disabled={busy} aria-label={`Delete chat: ${c.title}`} onClick={() => setConfirming(c.id)}>🗑</button>
                )}
              </li>
            ))}
          </ul>
          <p className="count">Deleting a chat removes its messages. Booked trips are kept.</p>
        </>
      )}
    </section>
  );
}
