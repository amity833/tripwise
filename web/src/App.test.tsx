import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { App } from './App';

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
beforeEach(() => { Element.prototype.scrollIntoView = vi.fn(); });
afterEach(() => vi.restoreAllMocks());

it('19. shows an error banner when sending fails, and Retry delivers the same message id', async () => {
  const chatBodies: { messageId: string }[] = [];
  let chatCalls = 0;
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    if (url === '/api/conversation') return json({ conversationId: 'conv-0001', messages: [] });
    if (url === '/api/chat') {
      chatBodies.push(JSON.parse(init!.body as string));
      if (++chatCalls === 1) throw new TypeError('network down');
      return json({ reply: { text: 'Here are **flights**' }, requestId: 'r1' });
    }
    return json({ error: 'nope' }, 404);
  }));

  render(<App />);
  const user = userEvent.setup();
  await user.type(await screen.findByLabelText('Message'), 'flights to Berlin');
  await user.click(screen.getByRole('button', { name: 'Send' }));

  const alert = await screen.findByRole('alert');
  expect(alert).toHaveTextContent(/Can't reach the server/);
  expect(screen.getByText('Failed to send')).toBeInTheDocument();

  await user.click(screen.getByRole('button', { name: 'Retry' }));
  await waitFor(() => expect(screen.getByText('flights')).toBeInTheDocument());
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  expect(chatBodies).toHaveLength(2);
  expect(chatBodies[1]!.messageId).toBe(chatBodies[0]!.messageId);
});

it('renders assistant markup as text, never as HTML', async () => {
  vi.stubGlobal('fetch', vi.fn(async (url: string) => url === '/api/conversation'
    ? json({ conversationId: 'conv-0001', messages: [{ role: 'assistant', text: 'hi <img src=x onerror=alert(1)>' }] }) : json({}, 404)));
  const { container } = render(<App />);
  await screen.findByRole('log');
  await waitFor(() => expect(container.querySelector('.msg.assistant')).not.toBeNull());
  expect(container.querySelector('img')).toBeNull();
});

it('reports the card on screen, and a typed yes updates that card in place', async () => {
  const bodies: { seenProposal?: string }[] = [];
  const pendingProposal = { token: 'abc123', status: 'proposed', createdAt: 1, payload: { flights: [], hotel: null, totalUsd: 0, policy: { verdict: 'pass', reasons: [] } } };
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    if (url === '/api/conversation') return json({ conversationId: 'conv-0001', messages: [], pendingProposal });
    bodies.push(JSON.parse(init!.body as string));
    return json({ reply: { text: 'Booked', proposal: { ...pendingProposal, status: 'confirmed' } }, requestId: 'r1' });
  }));

  render(<App />);
  const user = userEvent.setup();
  await screen.findByText('PROPOSED');
  await user.type(screen.getByLabelText('Message'), 'yes');
  await user.click(screen.getByRole('button', { name: 'Send' }));
  await screen.findByText('Booked');
  expect(bodies[0]!.seenProposal).toBe('abc123');
  // The card on screen is updated in place: one card, no leftover PROPOSED or buttons.
  expect(screen.getAllByRole('region', { name: /Trip proposal/ })).toHaveLength(1);
  expect(screen.getByText('BOOKED ✅')).toBeInTheDocument();
  expect(screen.queryByText('PROPOSED')).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /Confirm/ })).not.toBeInTheDocument();
});

it('closes the older card as REPLACED when a new proposal appears', async () => {
  const card = (token: string, createdAt: number) => ({ token, createdAt, status: 'proposed', payload: { flights: [], hotel: null, totalUsd: createdAt, policy: { verdict: 'pass', reasons: [] } } });
  vi.stubGlobal('fetch', vi.fn(async (url: string) => url === '/api/conversation'
    ? json({ conversationId: 'conv-0001', messages: [], pendingProposal: card('old111', 1) })
    : json({ reply: { text: 'New option', proposal: card('new222', 2) }, requestId: 'r1' })));

  render(<App />);
  const user = userEvent.setup();
  await screen.findByText('PROPOSED');
  await user.type(screen.getByLabelText('Message'), 'book the other one');
  await user.click(screen.getByRole('button', { name: 'Send' }));
  await screen.findByText('REPLACED');
  const [older, newer] = screen.getAllByRole('region', { name: /Trip proposal/ });
  expect(older).toHaveAccessibleName('Trip proposal, REPLACED');
  expect(newer).toHaveAccessibleName('Trip proposal, PROPOSED');
  expect(screen.getAllByRole('button', { name: /Confirm/ })).toHaveLength(1);
});
