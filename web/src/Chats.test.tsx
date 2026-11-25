import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { App } from './App';

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
const view = (id: string, text?: string) => ({ conversationId: id, messages: text ? [{ role: 'user', text }] : [] });
const list = (...titles: [string, string][]) => ({ limit: 2, conversations: titles.map(([id, title]) => ({ id, title, createdAt: 1, active: false })) });

beforeEach(() => { Element.prototype.scrollIntoView = vi.fn(); });
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it('shows the chat list at the limit, deletes after confirmation, and goes back to an earlier chat', async () => {
  let chats = list(['conv-0001', 'Berlin trip'], ['conv-0002', 'London trip']);
  const calls: string[] = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    const id = init?.body ? (JSON.parse(init.body as string) as { conversationId: string }).conversationId : '';
    calls.push(`${url} ${id}`.trim());
    if (url === '/api/conversation') return json(view('conv-0002', 'to London'));
    if (url === '/api/conversations') return json(chats);
    if (url === '/api/conversation/new') return json({ error: 'You already have 2 chats. Delete one to start a new one.' }, 409);
    if (url === '/api/conversation/open') return json(view(id, id === 'conv-0001' ? 'to Berlin' : 'to London'));
    if (url === '/api/conversation/delete') { chats = list(['conv-0001', 'Berlin trip']); return json(view('conv-0001', 'to Berlin')); }
    return json({}, 404);
  }));

  render(<App />);
  const user = userEvent.setup();
  await screen.findByText('to London');
  await user.click(screen.getByRole('button', { name: 'New chat' }));
  const panel = await screen.findByRole('region', { name: 'Your chats' });
  expect(within(panel).getByRole('status')).toHaveTextContent('Delete one');

  await user.click(await within(panel).findByRole('button', { name: 'Delete chat: London trip' }));
  expect(calls.some((c) => c.startsWith('/api/conversation/delete'))).toBe(false);
  await user.click(within(panel).getByRole('button', { name: 'Delete' }));
  await screen.findByText('to Berlin');
  await waitFor(() => expect(within(panel).getByText('1 of 2 chats')).toBeInTheDocument());

  await user.click(within(panel).getByRole('button', { name: 'Berlin trip' }));
  expect(screen.queryByRole('region', { name: 'Your chats' })).not.toBeInTheDocument();
});

it('does not show a late reply inside a different chat', async () => {
  let release!: () => void;
  const gate = new Promise<void>((r) => { release = r; });
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (url === '/api/conversation') return json(view('conv-0001'));
    if (url === '/api/chat') { await gate; return json({ reply: { text: 'Berlin answer' }, requestId: 'r' }); }
    if (url === '/api/conversations') return json(list(['conv-0001', 'Berlin trip'], ['conv-0002', 'London trip']));
    if (url === '/api/conversation/open') return json(view('conv-0002', 'to London'));
    return json({}, 404);
  }));

  render(<App />);
  const user = userEvent.setup();
  await user.type(await screen.findByLabelText('Message'), 'to Berlin');
  await user.click(screen.getByRole('button', { name: 'Send' }));
  await user.click(screen.getByRole('button', { name: 'Chats' }));
  await user.click(await screen.findByRole('button', { name: 'London trip' }));
  await screen.findByText('to London');
  expect(screen.queryByRole('status', { name: 'Assistant is typing' })).not.toBeInTheDocument();

  release();
  await new Promise((r) => setTimeout(r, 20));
  expect(screen.queryByText('Berlin answer')).not.toBeInTheDocument();
});

it('explains when the demo is full', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => json({ error: 'The demo is full right now.' }, 503)));
  render(<App />);
  expect(await screen.findByRole('alert')).toHaveTextContent('The demo is full');
});
