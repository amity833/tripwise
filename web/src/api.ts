export interface Flight { id: string; origin: string; destination: string; date: string; departTime: string; arriveTime: string; airline: string; cabin: string; priceUsd: number }
export interface Hotel { name: string; stars: number; checkIn: string; checkOut: string; nights: number; totalUsd: number }
export interface Proposal {
  token: string; status: 'proposed' | 'confirmed' | 'cancelled' | 'expired' | 'superseded'; createdAt: number;
  payload: { flights: Flight[]; hotel: Hotel | null; totalUsd: number; policy: { verdict: string; reasons: string[] } };
}
export interface Reply { text: string; proposal?: Proposal; quickReplies?: string[] }
export interface ChatMessage { role: 'user' | 'assistant'; text: string }
export interface Trip { reference: string; totalUsd: number; flights: Flight[]; hotel: Hotel | null }

export interface ConversationView { conversationId: string; messages: ChatMessage[]; pendingProposal?: Proposal }
export interface ChatSummary { id: string; title: string; createdAt: number; active: boolean }
export interface ChatList { limit: number; conversations: ChatSummary[] }

export class ApiError extends Error {
  constructor(message: string, public status = 0, public requestId?: string) { super(message); }
}

const KEY = 'tripwise.session';
export function sessionId(): string {
  try {
    let id = localStorage.getItem(KEY);
    if (!id) { id = crypto.randomUUID(); localStorage.setItem(KEY, id); }
    return id;
  } catch { return memoryId ??= crypto.randomUUID(); }
}
let memoryId: string | undefined;

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, { method, headers: { 'content-type': 'application/json', 'x-session-id': sessionId() }, body: body ? JSON.stringify(body) : undefined });
  } catch {
    throw new ApiError("Can't reach the server. Check your connection and retry.");
  }
  const json = await res.json().catch(() => ({}));
  if (!res.ok) throw new ApiError((json as { error?: string }).error ?? `Request failed (${res.status})`, res.status, (json as { requestId?: string }).requestId);
  return json as T;
}

export const api = {
  conversation: () => request<ConversationView>('GET', '/api/conversation'),
  conversations: () => request<ChatList>('GET', '/api/conversations'),
  newConversation: () => request<ConversationView>('POST', '/api/conversation/new'),
  openConversation: (conversationId: string) => request<ConversationView>('POST', '/api/conversation/open', { conversationId }),
  deleteConversation: (conversationId: string) => request<ConversationView>('POST', '/api/conversation/delete', { conversationId }),
  chat: (conversationId: string, messageId: string, text: string, seenProposal?: string) =>
    request<{ reply: Reply; requestId: string }>('POST', '/api/chat', { conversationId, messageId, text, seenProposal }),
  action: (conversationId: string, token: string, action: 'confirm' | 'cancel' | 'change') =>
    request<{ ok: boolean; toast: string; reply: Reply }>('POST', '/api/action', { conversationId, token, action }),
  trips: () => request<{ trips: Trip[] }>('GET', '/api/trips'),
  simulateOutage: (conversationId: string) => request<{ ok: boolean }>('POST', '/api/simulate-outage', { conversationId }),
};
