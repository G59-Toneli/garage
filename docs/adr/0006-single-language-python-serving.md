# Serving is a single-language Python monolith

The author's professional stack is .NET, and a .NET API with a Python sidecar for the machine
learning work would have been the more conventional showcase of service boundaries. It was rejected:
the embedding and fine-tuning ecosystem is Python regardless, so that shape pays for two runtimes,
two deployments and an internal HTTP hop on a free single-VM deployment — spending the portability
constraint of ADR-0001 to demonstrate a boundary the system does not need.

Serving is therefore **FastAPI**, structured as a modular monolith whose seams are the `Retriever`,
`Embedder`, `Reranker` and `Generator` interfaces. The frontend is a static build served by the same
container. Ingestion and fine-tuning are separate Python entry points that run offline.

## Consequences

- Python is pinned to **3.12** in the container. Newer interpreters outrun `torch` and
  `sentence-transformers` wheel availability, and the ARM target narrows this further.
- Model training happens off the VM on free GPU capacity and publishes the model as a versioned
  artifact; the VM only ever runs inference on CPU.
- The architectural argument of this project rests on the interfaces above, not on process
  boundaries. If a seam is ever unclear, the fix is a better interface, not a new service.
