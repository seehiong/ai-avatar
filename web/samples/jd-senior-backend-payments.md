Senior Backend Engineer — Payments Platform
Finlo (fictional company) · Singapore / Hybrid (3 days in office) · Full-time

About Finlo
Finlo is a Series C fintech that lets 40,000+ small and mid-sized merchants across Southeast Asia accept cards, QR payments (PayNow, DuitNow, PromptPay) and bank transfers through a single API. We process about S$4B in annual payment volume and are growing 3x year on year.

About the team
The Payments Platform team (7 engineers, 1 EM, 1 PM) owns the core money-movement path: payment authorisation, capture and refunds, the double-entry ledger, settlement to merchants, and reconciliation with our acquiring banks and payment service providers (PSPs). Our services are written in Go, communicate over gRPC and Kafka, store state in PostgreSQL, and run on Kubernetes (AWS EKS).

What you'll do
- Design, build and operate high-throughput, low-latency payment services in Go (peak ~3,000 transactions per second, p99 authorisation latency target under 250 ms).
- Own correctness of money movement: idempotent APIs, exactly-once effects on top of at-least-once messaging, retries with backoff, and safe handling of timeouts from downstream PSPs.
- Evolve our double-entry ledger and the daily reconciliation pipeline that matches our records against bank and PSP settlement files; drive unmatched items toward zero.
- Lead the migration of the remaining refund and payout flows from our legacy Python monolith to Go services with zero downtime.
- Raise reliability toward our 99.99% availability SLO: define SLIs, improve observability (Prometheus, Grafana, OpenTelemetry), take part in a follow-the-sun on-call rotation, and run blameless postmortems.
- Partner with Risk and Compliance on PCI-DSS scope reduction (tokenisation, network segmentation) and audit evidence.
- Write and review technical design documents (RFCs); mentor mid-level engineers through code review, pairing and design reviews.

What we're looking for (must have)
- 6+ years of professional backend engineering experience, including 2+ years building production services in Go (or strong Java/Kotlin with willingness to switch).
- Solid experience with event-driven systems (Kafka or similar): partitioning, consumer groups, ordering guarantees, retries and dead-letter handling.
- Deep PostgreSQL knowledge: schema design, transactions and isolation levels, indexing, query tuning, and safe online migrations.
- Proven understanding of distributed-systems fundamentals: consistency trade-offs, idempotency, sagas/outbox pattern, back-pressure, failure modes.
- Experience running services on Kubernetes in production, including resource tuning and debugging.
- A track record of leading a significant technical initiative end to end, and of making other engineers better.

Nice to have
- Payments or fintech domain experience: card schemes, PSP integrations (Adyen, Stripe, local acquirers), chargebacks, settlement and reconciliation.
- PCI-DSS, SOC 2 or MAS TRM exposure.
- Experience with Terraform, ArgoCD and AWS (EKS, RDS, MSK).
- Experience designing ledgers or other financial systems of record.

Interview process
1. 30-min recruiter call
2. 60-min technical deep dive on a system you built (this interview)
3. 90-min system design: "design a payout service"
4. 60-min behavioral / values with the Engineering Manager
5. Final chat with the Head of Engineering

What we offer
Competitive salary + equity, flexible hybrid work, S$2,000 yearly learning budget, comprehensive medical and dental, 20 days annual leave.
