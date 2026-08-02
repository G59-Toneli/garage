# Garage

[English](README.md) · **Português (BR)**

Um benchmark de recuperação reproduzível e uma demo caixa-de-vidro, construídos sobre um corpus a
respeito de um carro só: um Chevrolet Kadett GSi 1993.

> Tradução do [README em inglês](README.md), que é a versão canônica. Em caso de divergência, vale o
> inglês.

---

## O que é isto

O Garage responde perguntas técnicas sobre um único carro e mostra o próprio trabalho. Você pergunta
e ele não apenas responde: exibe os documentos em que procurou, os trechos que puxou, o score que
cada um recebeu e quanto tempo cada etapa levou. Mude o jeito de buscar e a resposta nova aparece ao
lado da antiga, para você ver o que a mudança comprou.

O produto não é responder. O produto é que a qualidade de um sistema de resposta pode ser **medida**,
e não afirmada. A tese do projeto é que uma feature de IA é um sistema com contrato de qualidade —
algo em que se coloca um número, que se protege contra regressão no CI e que se audita depois — e não
um prompt que pareceu funcionar quando alguém testou.

Um carro velho é um assunto propositalmente difícil. O material útil é um manual de serviço
escaneado e posts de fórum escritos em jargão de oficina; a pergunta que uma pessoa de verdade faz
("dá pra fazer swap de 250-S?") quase não compartilha vocabulário com o manual que a responde. Nada
disso está confortavelmente presente no treino de um modelo genérico, e é exatamente por isso que o
teste é justo.

**Fora de escopo**, em definitivo: mais de um veículo, contas de usuário, histórico de conversa
multi-turno, upload de documento pelo visitante, aplicativo móvel e qualquer monetização.

## Situação: Fase 1 de 5

O projeto entrega em cinco fatias verticais, cada uma terminando em um write-up público.

| Fase | Fatia | Estado |
|---|---|---|
| 0 | Spike de corpus — reunir material tier A e escrever à mão 20 perguntas de fato com resposta exata. Gate do projeto. | concluída |
| **1** | **Baseline honesto** — ingestão → Postgres/pgvector → `lexical` vs `dense` → gate determinístico no CI → UI de duas colunas com traces → deploy público. | **em andamento** |
| 2 | Híbrido e reranking — RRF em SQL, reranker hospedado, mais colunas, painel avançado. | planejada |
| 3 | Tier B e contrato de citação — ingestão de comunidade com atribuição, filtro de tier, juiz calibrado, conjunto de abstenção. | planejada |
| 4 | Fine-tune do embedder — pares sintéticos, mineração de negativos difíceis, segundo `model_key`, ganho medido pelo gate. | planejada |
| 5 | Série histórica e fechamento — dashboard, README consolidado, retrospectiva. | planejada |

**Pronto até aqui:** o documento de design, o vocabulário do domínio, as decisões de arquitetura, o
esqueleto do projeto — um serviço FastAPI, Postgres com pgvector no Compose e CI rodando os testes
mais um build de imagem multi-arquitetura; a ingestão — um comando reconstrói o banco inteiro a
partir de um corpus verificado, com chunking consciente de estrutura
([docs/ingestion.md](docs/ingestion.md)); e o primeiro caminho ponta a ponta — `POST /query` devolve
chunks ranqueados com score, tier, documento e página, mais a árvore de spans por trás deles, servido
por recuperação lexical sem nenhum modelo de linguagem no caminho
([docs/retrieval.md](docs/retrieval.md)).

**Ainda não construído:** recuperação densa e híbrida, o gate de avaliação, a UI
comparativa e o deploy público. Não há números de benchmark neste README porque ainda não existe
registro de corrida de onde derivá-los. Quando existir, cada número vai linkar para a corrida que o
produziu.

## Como funciona

```
                     ┌───────────────────────────────────────┐
  material local ──▶ │ ingest/  (Python, offline)            │
    (o seu)          │  manifest → verificação de hash       │
                     │  extração → normalização de jargão    │
                     │  chunking consciente de estrutura     │
                     │  embedding (baseline | fine-tunado)   │
                     └──────────────────┬────────────────────┘
                                        │ build determinístico
                                        ▼
                     ┌───────────────────────────────────────┐
                     │ Postgres + pgvector  (artefato)       │
                     │  somente-leitura em runtime           │
                     │  boot valida corpus_hash              │
                     └──────────────────┬────────────────────┘
                                        │
   ┌────────────────────────────────────┼────────────────────────────────┐
   │ serve/  (FastAPI, monolito modular)                                 │
   │                                                                     │
   │   Retriever  ◀interface▶  lexical │ dense │ hybrid (RRF)            │
   │   Reranker   ◀interface▶  none │ hospedado                          │
   │   Generator  ◀interface▶  contrato de citação, abstenção explícita  │
   │   Tracer     ─ spans OpenTelemetry por etapa                        │
   └────────────────────────────────────┬────────────────────────────────┘
                                        ▼
                     ┌───────────────────────────────────────┐
                     │ web/  (build estático)                │
                     │  colunas comparativas · chunks+scores │
                     │  trace · aba Evals · "re-rodar agora" │
                     └───────────────────────────────────────┘
```

Cada interface é um módulo profundo: contrato pequeno, implementação substituível, testável em
isolamento contra um corpus sintético.

- **`Retriever`** — `retrieve(query, k, filters) -> list[Candidate]`. `lexical` (`tsvector` +
  `pg_trgm`), `dense` (pgvector `<=>`), `hybrid` (RRF em SQL).
- **`Embedder`** — `embed(texts) -> matrix`. A *mesma* implementação roda na ingestão e na consulta;
  divergência aí é a fonte clássica de bug silencioso de recuperação.
- **`Reranker`** — `rerank(query, candidates) -> list[Candidate]`.
- **`Generator`** — `generate(query, context, contract) -> Answer`, devolvendo afirmações com citação
  obrigatória e sinal explícito de abstenção.

**O banco é artefato derivado, não a fonte da verdade.** A fonte é `corpus/v1/`; o banco é construído
a partir dela por um pipeline determinístico e nada escreve nele em runtime. O serviço valida o
`corpus_hash` no boot e recusa subir se ele divergir do commit. É isso que reconcilia "banco de
verdade" com reprodutibilidade: o banco é hasheável porque é reconstruível. Ver
[ADR-0002](docs/adr/0002-database-as-derived-artifact.md).

## O corpus, e por que o material-fonte não está aqui

O material mais útil sobre este carro é um manual de serviço da GM e posts escritos por pessoas em
fóruns. Tudo isso é protegido por direito autoral, e um repositório público que distribui cópias está
infringindo. Fatos, porém, não são protegidos — a expressão é.

Então este repositório publica:

- `corpus/v1/manifest.yaml` — por documento: identificador, título, editora, ano, procedência,
  `sha256` do arquivo original, tier e estado de direitos.
- `corpus/v1/facts/` — fatos extraídos em formato estruturado, cada um apontando para documento e
  página.
- `corpus/v1/excerpts/` — trechos curtos com atribuição, apenas de fontes de comunidade.
- `ingest/` — o script determinístico que reconstrói o corpus a partir dos seus arquivos locais.

O que ele nunca publica são os documentos. Quem clona aponta o pipeline para as próprias cópias; o
manifest verifica cada hash e recusa arquivo divergente. Ver
[ADR-0003](docs/adr/0003-no-redistribution-of-source-material.md).

As fontes são classificadas por **tier**, e o rótulo de tier fica visível em toda citação na
interface — manual de serviço e post de fórum nunca podem se parecer na tela:

- **Tier A** — verificável contra uma publicação nomeada: manual de serviço, manual do proprietário,
  catálogo de peças, ficha técnica publicada.
- **Tier B** — comunidade: fórum, blog, grupo. Conhecimento real que não existe em nenhum outro
  lugar, com autoridade menor.

O chunking é **consciente de estrutura**. Tabela de especificação é fatiada por linha, para que uma
especificação nunca seja cortada ao meio; procedimento é fatiado por passo; prosa é fatiada por
parágrafo, com sobreposição. `python -m garage ingest` reconstrói o banco inteiro a partir de um
corpus verificado, em uma única transação — veja [docs/ingestion.md](docs/ingestion.md).

## Eixos de configuração

Uma **configuração** é uma combinação concreta das escolhas do pipeline. Os eixos se dividem por uma
pergunta só: mudar este eixo custa um índice?

**Build-time** (caro — reconstrói o índice)

- estratégia de chunking — uma só no MVP
- embedder — `baseline` | `finetuned`

**Runtime** (livre — é um `WHERE`, não um redeploy)

- estratégia — `lexical` | `dense` | `hybrid`
- reranker — `none` | hospedado
- filtro de tier — `A` | `A+B`
- contrato de prompt — `citação obrigatória` | `livre` (o segundo existe só para demonstrar o
  contraste, e nunca é o padrão)

Dois embedders convivem em uma única tabela `embeddings` via `model_key`, o que só é possível porque
o modelo fine-tunado é derivado do baseline e preserva a dimensão dele. A interface expõe quatro
**presets** nomeados na frente e um painel avançado para combinação manual. Ver
[ADR-0005](docs/adr/0005-build-time-vs-runtime-axes.md).

## Avaliação

Avaliação aqui é cidadã de primeira classe, não script solto. Roda em duas camadas
([ADR-0004](docs/adr/0004-two-layer-evaluation.md)):

| Camada | Mede | Usa LLM? | Quando |
|---|---|---|---|
| **Gate determinístico** | `recall@k`, MRR, nDCG — só recuperação | não | todo commit, no CI, segundos, custo zero |
| **Avaliação de geração** | groundedness, precisão de citação, abstenção correta | sim, um juiz | sob demanda, local; saída commitada |

O gate quebra o build em regressão de recuperação. É onde vive o argumento do fine-tune, e não
precisa de nenhuma chamada de API para rodar.

Três conjuntos de avaliação:

- `eval/facts.jsonl` — pergunta → valor exato mais o `chunk_id` correto. Medido por match numérico
  com tolerância declarada, mais `recall@k`.
- `eval/recipes.jsonl` — pergunta aberta → rubrica. Medido por groundedness, precisão de citação e
  abstenção correta.
- `eval/abstention.jsonl` — perguntas deliberadamente *fora* do corpus. Acerto é recusar responder.
  **Abstenção é sucesso de primeira classe, não falha.**

**Não-determinismo é reportado, nunca escondido.** Nenhum valor pontual é publicado para a camada
estocástica: cada pergunta roda `k` vezes e a interface mostra média e dispersão. Todo resultado é um
**registro de corrida** — `run_id`, `git_sha`, `corpus_hash`, configuração, identidade do modelo,
temperatura, modelo juiz, versão do prompt, `n`, timestamps, métricas e detalhe por item — gerado por
execução e nunca escrito à mão. Qualquer número na interface sem rastro até um registro de corrida é
bug.

**Dois compromissos sobre o juiz, declarados aqui porque são os mais fáceis de quebrar em silêncio:**

1. O juiz é **cross-family** em relação ao gerador — um modelo de outro fornecedor corrige as
   respostas, para reduzir viés de auto-preferência.
2. **Juiz sem calibração não conta.** Cerca de 20 itens são rotulados à mão pelo autor e a taxa de
   concordância juiz↔humano é publicada. Sem esse número, nenhuma métrica de geração é reportada.

E um sobre contaminação: **os conjuntos de avaliação são escritos por humano e nunca gerados pelo
mesmo modelo que produz dados de treino.** A procedência fica registrada no próprio arquivo.

## Rodando localmente

```sh
docker compose up -d postgres
docker compose run --rm serve python -m garage ingest   # construa o artefato primeiro
docker compose up --wait
```

Postgres com pgvector instalado, o corpus fixture ingerido e o serviço respondendo em
<http://localhost:8000/health> e <http://localhost:8000/query>
([docs/retrieval.md](docs/retrieval.md)). O passo de ingestão não é opcional: o serviço valida o
`corpus_hash` no boot e recusa subir contra um banco que não seja o artefato deste commit.
Rode a suíte com `docker compose exec serve pytest`.
Toda configuração é lida do ambiente e todas têm um default que funciona; copie `.env.example` para
`.env` apenas quando quiser mudar alguma. Nada secreto é commitado.

O Python está fixado em **3.12** no container
([ADR-0006](docs/adr/0006-single-language-python-serving.md)), então o container é a forma suportada
de rodar a suíte — um interpretador local mais novo passa na frente dos wheels de machine learning de
que este projeto vai precisar. A imagem é construída para `linux/arm64` além de `linux/amd64`, porque
o alvo de deploy é uma VM ARM gratuita
([ADR-0001](docs/adr/0001-architecture-characteristics.md)).

## Organização do repositório

```
compose.yaml            Postgres + o serviço; a única forma suportada de rodar
Dockerfile              imagem multi-arch, Python fixado em 3.12
src/garage/             o serviço: configuração, app ASGI, módulos do pipeline
tests/                  suíte pytest, rodada no CI contra um Postgres de verdade
docker/initdb/          extensões instaladas no primeiro boot do banco
corpus/                 manifest, fatos extraídos, excertos — nunca documentos-fonte
corpus/jargon.yaml      o vocabulário de oficina curado, termo → canônico
docs/adr/               decisões de arquitetura e o que as forçou
docs/superpowers/specs/ o documento de design completo
CONTEXT.md              o vocabulário do domínio
```

## O que dirige o design

Toda decisão técnica aqui deve ser derivável desta lista. Decisão que não deriva dela é preferência
pessoal e precisa ser marcada como tal
([ADR-0001](docs/adr/0001-architecture-characteristics.md)).

1. **Reprodutibilidade e auditabilidade** — todo número exibido tem rastro até um `corpus_hash`, um
   commit SHA e um registro de corrida. É a tese.
2. **Testabilidade** — a avaliação roda como gate de CI.
3. **Modificabilidade** — trocar retriever, embedder ou reranker sem tocar no resto.
4. **Observabilidade** — cada consulta emite uma árvore de spans com tempo, tokens, custo e
   candidatos por etapa, renderizada na própria interface. O trace *é* o produto.
5. **Portabilidade** — a mesma imagem roda no Windows do autor e numa VM ARM gratuita. Nenhum serviço
   gerenciado proprietário.
6. **Custo** — teto rígido em zero. Custo é requisito de primeira classe, não limitação.

**Explicitamente não são características deste sistema:** escalabilidade, alta disponibilidade,
elasticidade, segurança multiusuário e latência como SLO. Latência aqui é *observável*, não
compromisso.

## Onde mora o raciocínio

- **[Documento de design](docs/superpowers/specs/2026-08-01-garage-design.md)** — o design completo:
  escopo, estratégia de corpus, avaliação, interface, fases. Escrito em português; tudo que é público
  sai em inglês.
- **[CONTEXT.md](CONTEXT.md)** — o vocabulário do domínio. Termos como *corpus*, *fato*, *receita* e
  *abstenção* são usados com precisão no código e na documentação, e é aqui que estão definidos.
- **[docs/adr/](docs/adr/)** — as decisões de arquitetura:
  - [0001](docs/adr/0001-architecture-characteristics.md) — características de arquitetura e não-objetivos explícitos
  - [0002](docs/adr/0002-database-as-derived-artifact.md) — o banco é artefato derivado, não a fonte da verdade
  - [0003](docs/adr/0003-no-redistribution-of-source-material.md) — o repositório não redistribui material de terceiros
  - [0004](docs/adr/0004-two-layer-evaluation.md) — avaliação em duas camadas: gate determinístico no CI e juiz sob demanda
  - [0005](docs/adr/0005-build-time-vs-runtime-axes.md) — eixos de configuração separados por custarem ou não um índice
  - [0006](docs/adr/0006-single-language-python-serving.md) — serving é um monolito Python de linguagem única
  - [0007](docs/adr/0007-corpus-hash-and-ingest-version-are-separate.md) — identidade do corpus e regras de chunking são dois números, não um

## Idioma

Código, README, ADRs e write-ups em inglês. O corpus, o jargão e as perguntas de avaliação em
português — ali o vocabulário é o objeto de estudo, não ruído. A versão canônica deste README é a
[em inglês](README.md).

## Licença

O código é [MIT](LICENSE). Datasets derivados têm licença aberta declarada. Material de terceiros não
é redistribuído — ver [ADR-0003](docs/adr/0003-no-redistribution-of-source-material.md).
