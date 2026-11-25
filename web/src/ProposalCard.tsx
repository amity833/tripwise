import type { Proposal } from './api';

const LABEL: Record<Proposal['status'], string> = { proposed: 'PROPOSED', confirmed: 'BOOKED ✅', cancelled: 'CANCELLED', expired: 'EXPIRED', superseded: 'REPLACED' };
export const formatDate = (iso: string) => new Intl.DateTimeFormat('en-GB', { timeZone: 'UTC', weekday: 'short', day: 'numeric', month: 'short' }).format(new Date(iso + 'T00:00:00Z'));

export function ProposalCard({ p, busy, onAction }: { p: Proposal; busy: boolean; onAction: (a: 'confirm' | 'cancel' | 'change') => void }) {
  const { flights, hotel, totalUsd, policy } = p.payload;
  return (
    <section className={`card card-${p.status}`} aria-label={`Trip proposal, ${LABEL[p.status]}`}>
      <header><span className={`badge badge-${p.status}`}>{LABEL[p.status]}</span></header>
      <ul>
        {flights.map((f) => (
          <li key={f.id}>✈️ <strong>{f.origin} → {f.destination}</strong> · {formatDate(f.date)} {f.departTime}→{f.arriveTime}<br /><small>{f.airline}, {f.cabin}, ${f.priceUsd}</small></li>
        ))}
        {hotel && <li>🏨 <strong>{hotel.name}</strong> {'★'.repeat(hotel.stars)}<br /><small>{formatDate(hotel.checkIn)} → {formatDate(hotel.checkOut)} ({hotel.nights} nights), ${hotel.totalUsd}</small></li>}
      </ul>
      <p className="total">Total ${totalUsd}</p>
      <p className={policy.verdict === 'warn' ? 'policy warn' : 'policy ok'}>{policy.verdict === 'warn' ? `⚠️ Policy warning: ${policy.reasons.join(' ')}` : '✅ Within company policy'}</p>
      {p.status === 'proposed' && (
        <div className="actions">
          <button disabled={busy} className="primary" onClick={() => onAction('confirm')}>✅ Confirm</button>
          <button disabled={busy} onClick={() => onAction('change')}>✏️ Change</button>
          <button disabled={busy} onClick={() => onAction('cancel')}>❌ Cancel</button>
        </div>
      )}
    </section>
  );
}
