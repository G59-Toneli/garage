# Architecture characteristics and explicit non-goals

Every technical decision in this project must be derivable from a ranked list of architecture
characteristics, so that choices can be defended as consequences rather than preferences. The
drivers, in order, are **reproducibility/auditability**, **testability**, **modifiability**
(swappable pipeline components), and **observability**. **Portability** (must run identically on a
developer laptop and a free ARM VM) and **cost** (hard ceiling at zero) are constraints, not
goals.

## Consequences

Scalability, high availability, elasticity, multi-user security, and latency-as-an-SLO are
explicitly **not** characteristics of this system. Latency is measured and displayed, but nothing is
promised. A reader who expects production hardening should read this list first — its absence is
deliberate, and optimising for it would trade away the drivers that justify the project.
