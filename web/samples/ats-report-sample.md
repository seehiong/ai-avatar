ATS match (sample): 71% for "Senior Backend Engineer — Payments Platform" at Finlo.
Strong matches: Go, Kafka, PostgreSQL, idempotency, transactional outbox, monolith-to-microservices migration, gRPC, mentoring, RFCs, on-call and incident leadership, 6+ years of experience.
Missing or weak:
- Kubernetes: listed as "familiar, used via platform team". The JD requires production Kubernetes experience, so probe hands-on depth.
- Double-entry ledger and reconciliation against bank or PSP settlement files: only adjacent experience (billing reconciliation job at DataNest).
- PCI-DSS, SOC 2 or MAS TRM: not mentioned.
- Card schemes, chargebacks and settlement: not mentioned. Some PSP integration experience (Stripe failover).
- Observability with OpenTelemetry or SLO definition: Prometheus and Grafana are listed, but no SLO or SLI work.
- Terraform, ArgoCD and EKS: Terraform is basic, and there is no ArgoCD or EKS.
Suggested probe areas: how the double-charge incident was fixed end to end; exactly-once effects with Kafka; online PostgreSQL migrations; leading the Python-to-Go migration; Kubernetes debugging; money-movement correctness.
