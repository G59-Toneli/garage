# Architecture characteristics and explicit non-goals

Every technical decision in this project must be derivable from a ranked list of architecture
characteristics, so that choices can be defended as consequences rather than preferences. The
drivers, in order, are **reproducibility/auditability**, **testability**, **modifiability**
(swappable pipeline components), and **observability**. **Portability** (must run identically on a
developer laptop and a free ARM VM) and **cost** (hard ceiling at zero) are constraints, not
goals.

## The target, stated in numbers

The deployment target is an **Oracle Cloud Ampere A1 instance on the always-free tier: 4 aarch64
vCPU and 24 GB of RAM**, in Vinhedo/SP. The figures are here because "a free ARM VM" is not a
resource budget and was twice read as one — two reviewers independently assumed 1 GB and spent real
time optimising a footprint that was never a constraint. Memory is not scarce on this machine; what
is scarce is money (zero) and what is *different* about it is the instruction set.

That second point is the one with consequences, and it is not about performance. Production runs on
**aarch64 while every number this project publishes is measured on x86-64**, and ONNX Runtime is not
bit-reproducible between the two (ADR-0008). Anything whose output a visitor could compare against a
published figure has to be held to that difference deliberately — measured, mitigated where a
mitigation exists, and stated where one does not.

## Consequences

Scalability, high availability, elasticity, multi-user security, and latency-as-an-SLO are
explicitly **not** characteristics of this system. Latency is measured and displayed, but nothing is
promised. A reader who expects production hardening should read this list first — its absence is
deliberate, and optimising for it would trade away the drivers that justify the project.
