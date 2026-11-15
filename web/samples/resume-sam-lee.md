SAM LEE (fictional sample resume)
Senior Backend Engineer · Singapore · sam.lee@example.com · github.com/example-samlee

SUMMARY
Backend engineer with 7 years of experience building high-throughput e-commerce and billing systems in Go and Python. Led a two-year migration from a Python monolith to Go microservices on Kafka. Comfortable owning services end to end, from design docs to on-call. Looking to go deeper into payments and financial correctness.

EXPERIENCE

ShopCo — Senior Backend Engineer (Orders & Checkout)            Mar 2021 – Present
E-commerce marketplace, ~2M monthly active buyers, 9,000 sellers.
- Led the migration of order management from a Python/Django monolith to 6 Go microservices with gRPC and Kafka, using the strangler-fig pattern and dual writes; completed with zero customer-facing downtime over 14 months.
- Designed the order-event pipeline on Kafka (24 partitions, ~40k events/s at peak sales) with a transactional outbox in PostgreSQL; reduced lost or duplicated order events from ~200/day to under 5/week.
- Cut checkout p99 latency from 900 ms to 120 ms by removing synchronous calls to inventory, adding request coalescing and read replicas.
- Introduced idempotency keys on the checkout API after a double-charge incident during a flash sale; wrote the postmortem and the internal guideline now used by 4 teams.
- Integrated two payment providers (Stripe and a regional wallet) behind a provider abstraction; built automatic failover when one provider's error rate exceeds 5%.
- Mentored 3 mid-level engineers (2 promoted); authored 9 RFCs and ran the team's weekly design review.
- On-call for checkout (tier-0); led incident response for the 2023 11.11 sale, peak 6x normal traffic.

DataNest — Backend Engineer                                     Jul 2018 – Feb 2021
B2B SaaS analytics platform.
- Built the usage-based billing service in Python (FastAPI) and PostgreSQL, generating ~12,000 invoices per month; designed the invoice line-item schema and monthly close job.
- Wrote a nightly job comparing billed usage against raw event counts; caught a metering bug worth ~S$80k/year in under-billing.
- Reduced the reporting query time for the largest customers from 45 s to 3 s through partitioning and covering indexes.
- Moved services from VMs to Docker on AWS ECS; set up CI/CD with GitHub Actions.

Bright Labs — Software Engineer (Graduate Program)               Jul 2017 – Jun 2018
- Built internal REST APIs in Java/Spring for a logistics client; added integration tests that cut regression bugs by about 30%.

SKILLS
Languages: Go (4 yrs), Python (7 yrs), Java (basic), SQL
Data & messaging: PostgreSQL, Redis, Kafka, gRPC, REST
Infrastructure: Docker, AWS (ECS, RDS, S3, SQS), GitHub Actions, Terraform (basic); Kubernetes (familiar, used via platform team)
Observability: Prometheus, Grafana, Sentry, structured logging

EDUCATION
B.Eng. Computer Engineering, National University of Singapore, 2017

CERTIFICATIONS
AWS Certified Developer – Associate (2022)
