// Common interview questions the interviewer can weave in at natural moments.
//
// Written for this project (not copied from any list), based on the themes that recur across
// interview-prep sources: STAR behavioral topics (decisions, conflict, failure, prioritisation,
// feedback), big-tech leadership-principle style prompts (disagree-and-commit, bias for action,
// dive deep), and engineering situational questions (incidents, technical debt, trade-offs).
//
// `when` tells the interviewer where the question fits:
//   early   - after the introduction, while building context
//   middle  - as a transition once a topic has been explored in depth
//   late    - near the end, before inviting the candidate's questions

export const QUESTION_CATEGORIES = {
  behavioral: {
    label: "Behavioral (STAR)", when: "middle",
    questions: [
      "Describe a time you had to make an important decision with limited or incomplete information. How did you decide, and how did it turn out?",
      "Tell me about a time you disagreed with your manager or a senior colleague. What did you do, and what happened after the decision was made?",
      "Tell me about a conflict with a teammate. How did you resolve it, and what would you do differently?",
      "Tell me about a project that failed or missed its goals. What was your part in it, and what did you learn?",
      "Describe a mistake you made at work that had real consequences. How did you handle it?",
      "Tell me about a time you had to deliver under a very tight deadline. What did you prioritise, and what did you let go?",
      "Tell me about a time you influenced a decision without having formal authority.",
      "Describe the most critical feedback you've received. What did you change because of it?",
      "Tell me about a time you took ownership of a problem that wasn't strictly your responsibility.",
      "Tell me about a time you had to learn a new technology or domain quickly to get something done.",
      "Describe a situation where the requirements were ambiguous or kept changing. How did you move forward?",
      "Tell me about a time you had to dig deep into data or details to find the real cause of a problem.",
      "Describe a time you had to act quickly when waiting for more information would have been costly.",
    ],
  },
  situational: {
    label: "Situational / hypothetical", when: "middle",
    questions: [
      "Imagine a production incident is affecting paying customers right now and you're on call. Walk me through the first thirty minutes.",
      "Suppose a bug shows up intermittently in production but you can't reproduce it locally. How would you approach it?",
      "You have one month and one other engineer to ship a customer-facing feature that realistically needs more. What do you do?",
      "A stakeholder insists on a feature you believe is risky for the system. How do you handle that conversation?",
      "You join a team and inherit a messy, poorly tested codebase that still has to ship features every sprint. What's your plan?",
      "Halfway through a project you realise the chosen architecture won't scale to next year's numbers. What do you do?",
    ],
  },
  engineering: {
    label: "Engineering judgement", when: "middle",
    questions: [
      "How do you decide which technical debt to pay down and which to live with? Give me a real example.",
      "Tell me about a corner you deliberately cut to hit a deadline. How did you communicate that trade-off, and did you come back to it?",
      "Describe a design review or code review where you pushed back strongly. What was the outcome?",
      "What's a technical decision you made that you'd reverse today, knowing what you know now?",
      "How do you make sure a system you own is observable and that you'd know about a problem before customers do?",
      "Tell me about the hardest bug you've tracked down. What made it hard?",
    ],
  },
  motivation: {
    label: "Motivation & fit", when: "early",
    questions: [
      "What made you interested in this role and this company specifically?",
      "Why are you looking to leave your current role?",
      "What kind of work energises you, and what kind drains you?",
      "Where would you like to be in your career in two or three years, and how does this role fit in?",
      "Describe the best team you've worked in. What made it work?",
    ],
  },
  leadership: {
    label: "Leadership (senior+)", when: "middle",
    questions: [
      "Tell me about someone you mentored. How did you help them grow, and how did you know it worked?",
      "Describe a time you had to deal with an underperforming teammate or a teammate who was struggling.",
      "Tell me about a time you set or changed the technical direction for a team. How did you get buy-in?",
      "How have you balanced delivery pressure from the business against engineering quality on a team you led?",
    ],
  },
  curveball: {
    label: "Curveballs", when: "late",
    questions: [
      "What would your current manager say is your biggest area for growth?",
      "Explain one of the systems you've built to someone with no technical background, in under a minute.",
      "Tell me about something you strongly believed about software that you've since changed your mind on.",
      "If you joined us, what would you focus on in your first ninety days?",
      "What's something on your resume you'd want me to ask about that I haven't yet?",
    ],
  },
};

// Default categories by interview type
export const DEFAULT_CATEGORIES = {
  mixed: ["behavioral", "situational", "motivation", "curveball"],
  behavioral: ["behavioral", "motivation", "leadership", "curveball"],
  technical: ["engineering", "situational", "curveball"],
  "system-design": ["engineering", "situational"],
};

/** Pick `count` bank questions from the enabled categories (fresh random draw per interview). */
export function pickQuestions(categories, count, seed = Date.now()) {
  let r = seed % 2147483647 || 1;
  const rand = () => (r = (r * 48271) % 2147483647) / 2147483647;
  const pool = [];
  for (const key of categories) {
    const cat = QUESTION_CATEGORIES[key];
    if (cat) for (const q of cat.questions) pool.push({ q, when: cat.when, cat: cat.label });
  }
  for (let i = pool.length - 1; i > 0; i--) { const j = Math.floor(rand() * (i + 1)); [pool[i], pool[j]] = [pool[j], pool[i]]; }
  // spread across categories: round-robin by category so 3 picks aren't all behavioral
  const byCat = new Map();
  for (const p of pool) (byCat.get(p.cat) || byCat.set(p.cat, []).get(p.cat)).push(p);
  const out = [], lists = [...byCat.values()];
  for (let i = 0; out.length < count && lists.some((l) => l.length); i++) {
    const l = lists[i % lists.length]; if (l.length) out.push(l.shift());
  }
  const order = { early: 0, middle: 1, late: 2 };
  return out.sort((a, b) => order[a.when] - order[b.when]);
}

export const SCENARIO_PRESETS = {
  "payments-backend": {
    label: "1. Payments Backend (Finlo — Senior Go/Kafka)",
    useSampleFiles: true,
    candidate: "Sam",
    role: "Backend Engineer (Payments Platform)",
    seniority: "Senior",
    company: "Finlo",
    type: "technical",
    difficulty: "standard",
    style: "warm",
    minutes: 10,
    rubricPreset: "general",
    focus: "payments correctness, Kafka, PostgreSQL, leading migrations",
    customQuestions: "",
    jd: `Senior Backend Engineer — Payments Platform (Finlo)
- Design and operate high-throughput payment services in Go (3,000 TPS peak, p99 < 250 ms).
- Own money-movement correctness: idempotency, outbox pattern, retries, ledger reconciliation.
- Lead Python-to-Go migrations with zero downtime and improve observability & Kubernetes reliability.`,
    resume: `SAM LEE — Senior Backend Engineer (7 yrs Go & Python)
- ShopCo (2021–Present): Led Python monolith -> 6 Go microservices migration with gRPC & Kafka (zero downtime, 14 months); built Kafka outbox pipeline (40k events/s); cut checkout p99 from 900 ms to 120 ms; added idempotency keys; mentored 3 engineers.
- DataNest (2018–2021): Built FastAPI + PostgreSQL usage-billing service (12,000 invoices/mo); caught S$80k/yr metering bug.`,
    ats: `ATS match: 71%. Probe gaps: hands-on Kubernetes debugging (listed as familiar via platform team), double-entry ledger reconciliation, and OpenTelemetry SLOs.`,
  },
  "bar-raiser-behavioral": {
    label: "2. Big-Tech Bar Raiser Loop (Behavioral)",
    candidate: "Jordan",
    role: "Senior Software Engineer (Core Infrastructure)",
    seniority: "Senior",
    company: "Northstar Cloud",
    type: "behavioral",
    difficulty: "tough",
    style: "neutral",
    minutes: 10,
    rubricPreset: "bar-raiser",
    focus: "Ownership, Dive Deep, Disagree and Commit, Bias for Action under ambiguity",
    customQuestions: "Tell me about a time you disagreed strongly with a senior leader's technical direction. How did you handle it?\nTell me about a critical mistake or missed commitment where you owned the fallout end to end.",
    jd: `Senior Software Engineer — Core Infrastructure (Northstar Cloud)
Responsibilities:
- Drive multi-team technical initiatives across compute scheduling and tenant isolation.
- Resolve high-ambiguity architectural disagreements using data and pragmatic trade-offs.
- Raise the engineering bar through rigorous postmortems, operational ownership, and mentoring.
Required: 5+ years production distributed systems, strong cross-functional influence, proven track record navigating high-stakes failures.`,
    resume: `JORDAN CHEN — Senior Software Engineer
Experience:
ApexStream (2021–Present) — Senior Engineer, Control Plane
- Owned multi-region deployment controller serving 1,800 enterprise clusters.
- Disagreed with VP proposal to build custom raft storage; benchmarked etcd vs FoundationDB, wrote a 6-page trade-off RFC that aligned 3 teams and saved ~4 engineering months.
- Led recovery after a bad config rollout caused a 22-minute US-East control-plane degradation; instituted staged canary rings and automated rollback gates, cutting blast radius by 90% on subsequent releases.
MetricPulse (2018–2021) — Software Engineer
- Built telemetry ingestion gateway in Go/Kafka handling 180k events/s; mentored 2 junior engineers.`,
    ats: `Probe focus: Ensure stories highlight personal ("I") decisions rather than team consensus; press for exact timeline, counterfactuals, and what data convinced stakeholders.`,
  },
  "system-design-scale": {
    label: "3. High-Scale System Design Pressure Test (Staff)",
    candidate: "Maya",
    role: "Staff Infrastructure Engineer",
    seniority: "Staff",
    company: "VeloPay",
    type: "system-design",
    difficulty: "tough",
    style: "direct",
    minutes: 25,
    rubricPreset: "system-design",
    focus: "capacity estimation, idempotency, multi-region failover, back-pressure, data modeling",
    customQuestions: "How would you design a multi-region distributed rate limiter and quota enforcement service handling 500k RPS with sub-10 ms p99 overhead?",
    jd: `Staff Infrastructure Engineer — Edge & Core Reliability (VeloPay)
- Architect low-latency edge gateways, quota enforcement, and multi-region failover topologies at 500k+ RPS.
- Define storage vs coordination trade-offs (consistency, hot partitions, failure modes, degraded operation).
- Lead technical reviews across 4 platform teams.`,
    resume: `MAYA PATEL — Principal/Staff Systems Engineer (9 yrs)
- CloudScale (2020–Present): Designed distributed API gateway in Rust/Envoy handling 350k RPS across 4 regions; cut tail latency p99 from 48 ms to 11 ms. Built Redis Cluster + local token-bucket hybrid limiter with async gossip sync.
- LedgerGrid (2016–2020): Built sharded PostgreSQL event store and async CDC replication pipeline.`,
    ats: `Probe focus: Push hard on failure modes (Redis partition, hot-key thundering herd, clock skew across regions, fail-open vs fail-closed trade-offs).`,
  },
  "incident-debugging": {
    label: "4. Production Incident & Debugging Under Fire (SRE)",
    candidate: "Devon",
    role: "Senior Reliability & Backend Engineer",
    seniority: "Senior",
    company: "PulseHealth",
    type: "technical",
    difficulty: "standard",
    style: "direct",
    minutes: 10,
    rubricPreset: "general",
    focus: "live incident triage, database lock contention, observability, blameless postmortems",
    customQuestions: "Walk me through the worst intermittent production bug you've ever diagnosed — how did you isolate the root cause when logs were inconclusive?",
    jd: `Senior Site Reliability & Backend Engineer (PulseHealth)
- Own 99.99% uptime SLO for clinical data ingestion and patient notification pipelines (PostgreSQL, Go, Kubernetes, Kafka).
- Lead live severity-1 incident triage, containment, root-cause elimination, and systemic safeguards.
- Improve OpenTelemetry tracing, saturation alerting, and graceful degradation.`,
    resume: `DEVON BROOKS — Senior Backend / SRE (6 yrs)
- CareFlow Systems (2021–Present): Primary on-call for HIPAA-regulated clinical messaging API (~1,200 RPS). Resolved cascading PostgreSQL connection pool exhaustion caused by unindexed JSONB queries under retry storms; added circuit breakers and pgbouncer transaction pooling, cutting p99 from 4.2 s to 95 ms.
- Built OpenTelemetry distributed tracing across 14 services and cut MTTR from 47 min to 14 min.`,
    ats: `Probe focus: Test hypothesis generation order during triage, containment vs root-cause prioritization, and concrete prevention mechanisms beyond "we added a monitor".`,
  },
  "fullstack-product": {
    label: "5. Full-Stack Product Engineer (Growth & Core UX)",
    candidate: "Taylor",
    role: "Full-Stack Product Engineer",
    seniority: "Mid-level",
    company: "CanvasFlow",
    type: "mixed",
    difficulty: "standard",
    style: "warm",
    minutes: 10,
    rubricPreset: "behavioral-star",
    focus: "frontend performance, API contracts, A/B experimentation, product trade-offs",
    customQuestions: "Tell me about a time you pushed back on a product design or scope because of engineering or UX trade-offs. How did you align with PM/Design?",
    jd: `Full-Stack Product Engineer (CanvasFlow)
- Build responsive collaborative workflows in TypeScript, React/Next.js, and Node.js/PostgreSQL.
- Partner closely with PM and Design to iterate rapidly via A/B tests while maintaining Web Vitals and API reliability.
- Own features end to end from instrumentation and UX polish to backend schema migrations.`,
    resume: `TAYLOR NGUYEN — Full-Stack Software Engineer (4 yrs)
- StudioSync (2022–Present): Built real-time collaborative comment thread & optimistic UI in React/TypeScript + WebSockets; cut bundle size by 34% and INP from 280 ms to 65 ms. Built Node/PostgreSQL onboarding experiment service that lifted activation by 11%.
- PixelCart (2020–2022): Developed storefront checkout components and REST APIs in TypeScript and GraphQL.`,
    ats: `Probe focus: Verify depth on optimistic UI conflict resolution, end-to-end ownership of product metrics, and constructive PM collaboration.`,
  },
};
