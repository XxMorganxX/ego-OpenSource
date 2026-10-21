"""The judge seam.

THE DOCTRINE -- "structure may ADMIT; only the query, and query-conditioned
judgment, may RANK" -- is enforced by keeping this the ONLY thing that can say yes to
a frontier row. entry.py and frontier.py produce a MENU in structural order; nothing
upstream may shrink or reorder that menu based on what the query MEANS. A HopJudge is
the sole seam where the query enters an ADMISSION decision, as opposed to rank.py,
where the query enters the RANKING decision via L_sim/L_flat/L_admit.

DeterministicHopJudge is not a stand-in waiting to be replaced -- it is the
production fail-soft path: "the deterministic crawl is the same pipeline
with decide() replaced by a rule, not a second code path". It is now the
ONLY entry in HOP_JUDGES: the LLM tier that used to share this Protocol and this
HopDecision shape (LLMHopJudge) was deleted after the 2x2 benchmark found it did not
earn its tens of seconds per hop (see the tombstone above HOP_JUDGES below). Only HOP_JUDGES[name]
ever differed between the two tiers, which is what kept the deletion the one-file
change the design anticipated.

DEFAULT HOP JUDGE IS "none", PERMANENTLY, a deliberate deviation from the original
design's DEFAULT_HOP_JUDGE = "llm". The reason is registry.py's own
stated principle (the DEFAULT_STRATEGY comment) -- a default must never silently opt a caller into a path whose
cost_class is "expensive" -- made concrete by ~15 existing tests that construct
get_strategy("cluster-crawl", kb=kb) unpinned and would each acquire a subprocess, a
quota slice and tens of seconds. THE PRODUCTION SURFACE WAS ONCE AGENTIC, while the LLM
tier existed: KB_MCP's search_graph tool passed judge="llm" explicitly, beside a docstring that
told an agent what it cost, so any MCP caller that omitted judge= reached
LLMHopJudge for real. That tier was deleted -- the 2x2 benchmark found
it did not earn its tens of seconds per hop (see the tombstone above HOP_JUDGES below) -- and search_graph's
own default flipped to "none" to match, so nothing MCP-side still claims an agentic
default this module no longer backs. The library default here stayed fail-soft and
CI-safe underneath that agentic surface the whole time, and stays so now that the
surface itself is gone too -- this was never a staging step, and there is no
constant left to "finish the job" by flipping.
"""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from kb_retrieval.contract import Query
from kb_retrieval.crawl.frontier import FRONTIER_ADMITTED, Frontier


@dataclass(frozen=True)
class HopDecision:
    """What a judge returns for one hop.

    `admit` is INDEX-ONLY -- positions into the offered Frontier, never a node id
    (prompt-injection containment). The worst a hostile note can achieve is
    getting ITSELF admitted, and it still has to survive rank.py's four-way fusion
    to reach a slot; index-only output caps the damage at self-admission. Indices
    outside the offered range are dropped by strategy.py and counted in
    trace.rejected_admissions.

    `stop` is ASYMMETRIC: a judge may only SHRINK the budget. stop=True ends the
    crawl immediately; stop=False is advisory and buys no hop beyond what
    strategy.py's own budget and marginal-yield checks already allow.

    Everything below `note` is DISCLOSURE, not decision -- the crawl's disclosure
    surface, carried on the decision because the hop is the only place that knows
    what happened. All defaulted, so DeterministicHopJudge and every test fake keep
    constructing HopDecision(admit=..., stop=...) unchanged:

      by                which TIER actually decided this hop -- "llm" or
                        "deterministic". Empty means "the judge did not say", and
                        strategy.py then falls back to the judge's own name. This is
                        NOT judge identity: an LLMHopJudge that fell back reports
                        "deterministic", which is exactly what keeps a mixed crawl
                        from being reported as agentic. The design's own
                        disclosure example already assumes this field exists -- its
                        two hops carry DIFFERENT `by` values ("llm", then
                        "deterministic" with "reason":"deadline").
      llm_calls         0 or 1, and counts DECISIONS, not attempts: it is 1 only when
                        a hop's reply parsed and this HopDecision is the model's own
                        (by="llm"). A hop whose call reached the provider but failed
                        to parse still reports llm_calls=0 -- the DECISION for that
                        hop is DeterministicHopJudge's, even though real provider
                        tokens were spent getting there. cost.llm_calls == 0 is
                        therefore the machine-checkable "this was not an agentic
                        DECISION" flag, not "this hop cost nothing": by=
                        "deterministic" and llm_calls==0 can still carry a non-None
                        provider_tokens / provider_cost_usd on the parse-failure
                        fallback (see _fallback's `client=` argument) -- complete()
                        returned normally, so those tokens were genuinely billed, and
                        silently dropping them would bias an agentic run's cost
                        column downward by exactly its failed hops. Historical note:
                        breaker.llm_gate() (deleted with the LLM tier) used
                        the OPPOSITE convention on purpose: it counted an ATTEMPT --
                        every granted gate, before the call was even made -- against
                        the hourly budget, because a failing provider still draws on
                        the shared quota and must not get unlimited free tries. Two
                        conventions for two questions: the historical llm_gate's budget
                        answered "how many times did we ask the provider"; HopDecision.
                        llm_calls answers "how many hops did the model actually decide".
                        Both were deliberate.
      provider_tokens   the SUM of the four scalar counts in the provider's usage
                        blob; None when the provider reported nothing. None rather
                        than 0, because 0 claims a measured zero. May be set even
                        when llm_calls==0 -- see `llm_calls` above.
      provider_cost_usd likewise, from usage["cost_usd"].
      llm_status        the human string for trace.llm ("ok", "unavailable (quota,
                        resets ...)", ...).
      degraded_key      the machine key for trace.degraded ("llm_quota",
                        "llm_circuit", "llm_budget", "llm_concurrency", "llm_error",
                        "llm_deadline", "llm_calls").
    """
    admit: "list[int]"
    stop: bool
    note: str = ""
    by: str = ""
    llm_calls: int = 0
    provider_tokens: "int | None" = None
    provider_cost_usd: "float | None" = None
    llm_status: "str | None" = None
    degraded_key: "str | None" = None


@runtime_checkable
class HopJudge(Protocol):
    name: str
    # Declared on the JUDGE, not derived from its name, because
    # ClusterCrawlStrategy.capabilities() reports it and a benchmark harness plans
    # its matrix from that. It is the planning-time twin of the runtime flag
    # cost.llm_calls == 0.
    uses_llm: bool

    # `admitted` is the "digest of the already-admitted pool": the TITLES of
    # rows this crawl has already admitted, in admission order. Keyword-only with a
    # default so the deterministic judge and every test fake ignore it, and STATELESS
    # -- passed in per call rather than accumulated on the judge, because one
    # strategy instance serves many searches (test_crawl_strategy.py reuses one for
    # three) and a judge that remembered the last query's admissions would leak
    # across them and break byte-identical-on-repeat.
    def decide(self, q: Query, frontier: Frontier, hop: int, *,
               admitted: "tuple[str, ...]" = ()) -> HopDecision: ...


class DeterministicHopJudge:
    """The no-LLM judge. Registered as "none", and DEFAULT_HOP_JUDGE today.

    Takes the first FRONTIER_ADMITTED offered rows in FRONTIER-MENU ORDER -- the
    order frontier.py already built them in, which is the primitives' own
    server-side ordinal -- and ALWAYS stops.

    "Always stops", rather than "stops after some heuristic", is what makes this the
    SAME pipeline as an LLM judge with one function swapped: the hop loop, the budget
    checks and the marginal-yield test in strategy.py never change; this judge simply
    never asks for hop 2. Hop 2's machinery is still built and still exercised by
    tests, because swapping in a judge that returns stop=False changes nothing else.

    It never reads `q`. The crawl still admits query-ROUTED rows -- concept and
    bridge membership was routed there BY the query's embedding -- but nothing here
    is query-CONDITIONED, which is exactly the gap the (deleted) LLM judge existed to
    close, and exactly what the 2x2 measures.
    """
    name = "none"
    uses_llm = False

    def decide(self, q: Query, frontier: Frontier, hop: int, *,
               admitted: "tuple[str, ...]" = ()) -> HopDecision:
        # `admitted` is ignored, deliberately and permanently: this judge is not
        # query-conditioned, and it is not pool-conditioned either. Reading it would
        # make the deterministic control arm depend on what the LLM arm had done in
        # an earlier hop.
        admitted_idx = list(range(min(len(frontier), FRONTIER_ADMITTED)))
        return HopDecision(
            admit=admitted_idx, stop=True,
            note=f"deterministic: first {len(admitted_idx)} of {len(frontier)} "
                 "offered")


# "llm" (LLMHopJudge + prompts.py + breaker.py) was DELETED after the
# 2x2 benchmark falsified it: lower recall on SET-FLAT, no precision gain, latency
# orders of magnitude above the deterministic crawl's.
# Resurrection path + retest trigger: negatives.py "agentic hop judge" entry.
HOP_JUDGES: "dict[str, type]" = {"none": DeterministicHopJudge}
# STAYS "none" -- a deliberate deviation from the original design's "llm". See this
# module's docstring. KB_MCP's search_graph used to
# pass judge="llm" explicitly, which is what made
# production agentic without making every unpinned caller expensive; when the tier was
# deleted, search_graph's own default flipped to "none" to match this module, so nothing
# MCP-side still claims an agentic default this module no longer backs.
DEFAULT_HOP_JUDGE = "none"


def get_judge(name: "str | None", *, client=None) -> HopJudge:
    """Resolve a judge by name, optionally injecting an inference client.

    `client` is passed to the class ONLY when it is not None, so judges that take no
    client (DeterministicHopJudge, and every test fake, all constructed with `cls()`)
    are unaffected -- and injecting a client into one of them raises a TypeError
    naming the class rather than being silently swallowed. The path in is
    ClusterCrawlStrategy(kb, judge="llm", judge_client=...), which registry.
    get_strategy forwards as **kw.
    """
    key = name or DEFAULT_HOP_JUDGE
    cls = HOP_JUDGES.get(key)
    if cls is None:
        known = ", ".join(sorted(HOP_JUDGES))
        raise ValueError(f"unknown hop judge {key!r}; registered: {known}")
    return cls() if client is None else cls(client=client)
