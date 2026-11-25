import asyncio
from datetime import UTC, datetime, timedelta

from tripwise.app import build_service
from tripwise.config import load_config
from tripwise.core.models import Ctx
from tripwise.core.policy import check_policy
from tripwise.core.proposals import PROPOSAL_TTL_MS, cancel_proposal, confirm_proposal, create_proposal, supersede_proposal
from tripwise.data.dates import add_days, now_ms, today_iso
from tripwise.data.flights import get_flight, search_flights
from tripwise.data.hotels import search_hotels
from tripwise.store.memory import MemoryRepo
from tripwise.tools import default_tools
from tripwise.tools.registry import ToolContext

NOW = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)
TZ = "UTC"
who = Ctx("web:a", "c1", "r")


class TestPolicyEngine:
    eco = search_flights("TLV", "BER", "2026-12-10", "economy")[0]
    biz = search_flights("TLV", "BER", "2026-12-10", "business")[0]

    def test_flights_are_deterministic_and_ids_resolve_back(self):
        assert search_flights("TLV", "BER", "2026-12-10", "economy") == search_flights("TLV", "Berlin", "2026-12-10", "economy")
        assert get_flight(self.eco.id) == self.eco
        assert get_flight("FL-XXX-YYY-20261210-1-Y") is None

    def test_passes_economy_and_capped_hotel(self):
        h = search_hotels("BER", "2026-12-10", "2026-12-12")[0]
        assert check_policy([self.eco.id], h.id, NOW, TZ).verdict == "pass"

    def test_hard_blocks_business_on_a_short_flight_and_suggests_economy(self):
        r = check_policy([self.biz.id], None, NOW, TZ)
        assert r.verdict == "block"
        assert r.suggestion and "economy flight FL-TLV-BER" in r.suggestion

    def test_allows_business_on_a_long_flight(self):
        lh = search_flights("TLV", "NYC", "2026-12-10", "business")[0]
        assert lh.durationMin > 360
        assert check_policy([lh.id], None, NOW, TZ).verdict == "pass"

    def test_hard_blocks_a_hotel_above_the_tier_cap(self):
        h = search_hotels("BER", "2026-12-10", "2026-12-12")[2]
        assert check_policy([], h.id, NOW, TZ).verdict == "block"

    def test_soft_warns_when_booked_less_than_7_days_ahead(self):
        near = search_flights("TLV", "BER", "2026-11-04", "economy")[0]
        r = check_policy([near.id], None, NOW, TZ)
        assert r.verdict == "warn"
        assert "3 day" in r.reasons[0]

    async def test_a_hallucinated_flight_id_is_a_block_never_a_proposal(self):
        assert check_policy(["FL-FAKE"], None, NOW, TZ).verdict == "block"
        repo = MemoryRepo()
        r = await create_proposal(repo, who, "FL-TLV-BER-20261210-9-Y", None, None, NOW, TZ)
        assert not r.ok
        assert await repo.latest_proposed(who.user_id, who.conversation_id) is None


class TestTools:
    @staticmethod
    async def run(name, args):
        return await default_tools().execute(name, args, ToolContext(who, MemoryRepo(), NOW, TZ))

    async def test_rejects_bad_input(self):
        assert "past" in (await self.run("search_flights", {"origin": "TLV", "destination": "BER", "date": "2026-10-01"}))["error"]
        r = await self.run("search_flights", {"origin": "TLV", "destination": "BER", "date": "2026-12-10", "return_date": "2026-12-09"})
        assert "before" in r["error"]
        assert "Unknown destination" in (await self.run("search_flights", {"origin": "TLV", "destination": "Mars", "date": "2026-12-10"}))["error"]
        assert "real calendar" in (await self.run("search_flights", {"origin": "TLV", "destination": "BER", "date": "2026-02-31"}))["error"]
        assert "Invalid arguments" in (await self.run("search_hotels", {"city": "BER", "check_in": "2026-12-10"}))["error"]

    async def test_returns_outbound_and_inbound_options(self):
        r = await self.run("search_flights", {"origin": "TLV", "destination": "BER", "date": "2026-12-01", "return_date": "2026-12-03"})
        assert len(r["outbound"]) == 4 and len(r["inbound"]) == 4
        assert r["resolved"]["departure"] == "Tue 1 Dec"


class TestProposals:
    @staticmethod
    async def setup():
        repo = MemoryRepo()
        f = search_flights("TLV", "BER", "2026-12-10", "economy")[0]
        r = await create_proposal(repo, who, f.id, None, None, NOW, TZ)
        assert r.ok and r.proposal
        return repo, r.proposal

    async def test_13_double_confirm_books_exactly_once(self):
        repo, p = await self.setup()
        a, b = await asyncio.gather(confirm_proposal(repo, p.token, who, NOW, TZ), confirm_proposal(repo, p.token, who, NOW, TZ))
        assert [a.ok, b.ok].count(True) == 1
        assert len(await repo.list_bookings(who.user_id)) == 1
        again = await confirm_proposal(repo, p.token, who, NOW, TZ)
        assert not again.ok and again.reason == "already_booked"

    async def test_14_token_from_conversation_a_is_rejected_in_b_and_for_other_users(self):
        repo, p = await self.setup()
        r1 = await confirm_proposal(repo, p.token, Ctx("web:a", "c2"), NOW, TZ)
        r2 = await confirm_proposal(repo, p.token, Ctx("web:evil", "c1"), NOW, TZ)
        assert r1.reason == "not_found" and r2.reason == "not_found"
        assert await repo.list_bookings("web:a") == []

    async def test_15_expired_proposal_gives_the_expiry_message(self):
        repo, p = await self.setup()
        late = NOW + timedelta(milliseconds=PROPOSAL_TTL_MS + 1000)
        r = await confirm_proposal(repo, p.token, who, late, TZ)
        assert not r.ok and r.reason == "expired" and "expired" in r.message
        assert await repo.list_bookings(who.user_id) == []

    async def test_confirm_rechecks_policy(self):
        repo, p = await self.setup()
        far_future = datetime(2026, 12, 20, tzinfo=UTC)
        p.expires_at = now_ms(far_future) + 10**6
        await repo.save_proposal(p)
        r = await confirm_proposal(repo, p.token, who, far_future, TZ)
        assert not r.ok and r.reason == "blocked"

    async def test_only_one_live_proposal_and_stale_buttons_are_harmless(self):
        repo, p = await self.setup()
        f2 = search_flights("TLV", "BER", "2026-12-11", "economy")[0]
        r2 = await create_proposal(repo, who, f2.id, None, None, NOW, TZ)
        assert (await repo.get_proposal(p.token)).status == "superseded"
        assert (await confirm_proposal(repo, p.token, who, NOW, TZ)).reason == "superseded"
        assert r2.proposal
        assert (await supersede_proposal(repo, r2.proposal.token, who)).ok
        assert (await cancel_proposal(repo, r2.proposal.token, who)).reason == "superseded"


async def test_dev_fake_end_to_end_policy_block_and_soft_warning_through_the_real_agent():
    repo = MemoryRepo()
    svc = build_service(load_config({}), repo)
    c = await svc.start_conversation("web:x")
    ctx = Ctx("web:x", c.id, "r")

    def say(t: str):
        return svc.handle_message(ctx, t, "t")

    today = today_iso(datetime.now(UTC), "UTC")
    far = add_days(today, 20)
    await say(f"business class to Berlin {far} {add_days(far, 2)}")
    blocked = await say("book the first one")
    assert blocked.proposal is None
    assert "Business class is not allowed" in blocked.text

    near = add_days(today, 3)
    await say(f"flights to Berlin {near} {add_days(near, 2)}")
    warned = await say("book the second one")
    assert warned.proposal and warned.proposal.status == "proposed"
    assert warned.proposal.payload["policy"]["verdict"] == "warn"
    assert "requestId" not in warned.proposal.to_dict()


def test_resolve_relative_dates():
    from tripwise.data.dates import resolve_relative

    assert resolve_relative("tomorrow", "2026-11-25") == ("2026-11-26", "tomorrow")
    assert resolve_relative("next month", "2026-11-25")[0] == "2026-12-07"  # first Monday
    assert resolve_relative("next month", "2026-12-20")[0] == "2027-01-04"  # year rollover
    assert resolve_relative("in 10 days", "2026-11-25")[0] == "2026-12-05"
    assert resolve_relative("in 1 month", "2027-01-31")[0] == "2027-02-28"  # clamps to month end
    assert resolve_relative("friday", "2026-11-27")[0] == "2026-12-04"  # never "today"
    assert resolve_relative("whenever", "2026-11-25") is None


def test_search_results_are_varied():
    from tripwise.data.flights import search_flights

    for date in ("2026-12-07", "2026-12-10", "2027-01-04"):
        fl = search_flights("TLV", "BER", date, "economy")
        assert len({f.airline for f in fl}) == 4
        assert len({f.departTime for f in fl}) == 4
