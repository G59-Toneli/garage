# Garage — Design

**Data:** 2026-08-01
**Status:** aprovado para planejamento
**Nome:** Garage (travado)

> Documento de trabalho em PT-BR. A documentação pública do repositório (README, ADRs, write-ups)
> é escrita em **inglês** — ver §16.

---

## 1. Visão

Sistema open source de perguntas e respostas sobre o **Chevrolet Kadett GSi 1993**, servido como
uma *caixa de vidro*: o visitante faz uma pergunta e vê, lado a lado, a mesma pergunta atravessando
configurações diferentes do pipeline de recuperação e geração, com as fontes citadas, os trechos
recuperados, os scores, o tempo e o custo de cada etapa.

O produto não é o chatbot. O produto é **a evidência de que cada decisão técnica foi medida**.

## 2. Público e objetivo

Três públicos, atendidos pelo mesmo artefato:

| público | o que precisa ver | onde vê |
|---|---|---|
| recrutador técnico / tech lead (BR) | profundidade e critério de decisão | demo + ADRs |
| mercado internacional / remoto | comunicação em inglês e rigor de engenharia | write-ups + repo |
| comunidade de IA | algo útil e reproduzível | benchmark + datasets + posts |

**Tese de posicionamento:** *tratar feature de IA como sistema de engenharia — contrato de qualidade
mensurável antes de escolher técnica.*

## 3. Características de arquitetura (drivers)

Toda decisão técnica deve ser derivável desta lista. Decisão que não derive daqui é preferência
pessoal e precisa ser marcada como tal.

**Dirigem o sistema:**

1. **Reprodutibilidade / auditabilidade** — todo número exibido tem rastro até `corpus_hash` +
   commit SHA + registro de corrida. É a tese do projeto.
2. **Testabilidade** — avaliação é cidadã de primeira classe: roda em gate de CI, não é script solto.
3. **Modificabilidade (pluggability)** — trocar retriever, embedder ou reranker sem tocar no resto.
4. **Observabilidade** — cada consulta emite árvore de spans com tempo, tokens, custo e candidatos
   por etapa. O trace é renderizado na própria UI (§12).

**Restrições duras:**

5. **Portabilidade / deployability** — roda idêntico no Windows do autor e numa VM ARM gratuita
   (Oracle Cloud, Vinhedo/SP). Elimina qualquer dependência de serviço gerenciado proprietário.
6. **Custo** — teto rígido em zero. Custo é requisito de primeira classe, não limitação.

**Explicitamente NÃO são características deste sistema:** escalabilidade, alta disponibilidade,
elasticidade, segurança multiusuário, latência como SLO. Latência é **observável**, não compromisso.

## 4. Escopo

**Dentro:** um veículo (Kadett GSi 1993); corpus híbrido tier A + tier B; recuperação léxica, densa,
híbrida e com reranking; embedder fine-tunado; avaliação em duas camadas; UI comparativa com traces;
deploy público.

**Fora:** múltiplos veículos, contas de usuário, histórico de conversa multi-turno, upload de
documento pelo visitante, aplicativo móvel, qualquer forma de monetização.

## 5. Linguagem do domínio

| termo | significado | evitar |
|---|---|---|
| **Corpus** | conjunto versionado e imutável de documentos-fonte, identificado por hash | "base de dados" |
| **Tier A** | fonte técnica verificável: manual de serviço, manual do proprietário, catálogo de peças, ficha técnica | "fonte oficial" |
| **Tier B** | fonte de comunidade: fórum, blog, grupo. Conhecimento real, autoridade menor | "fonte não confiável" |
| **Fato** | afirmação com valor exato e verificável (torque, folga, medida, código de peça) | "dado" |
| **Receita** | procedimento ou recomendação sem resposta única ("projetinho de rua") | "opinião" |
| **Jargão** | vocabulário de oficina/comunidade não presente em texto formal ("swap 250-S") | "gíria" |
| **Configuração** | combinação concreta dos eixos do pipeline usada para responder | "modelo" |
| **Registro de corrida** | artefato gerado por execução de avaliação, com metadados de proveniência | "resultado" |
| **Abstenção** | recusa correta em responder quando o corpus não cobre a pergunta | "erro" |

## 6. Corpus

### 6.1 Restrição jurídica (decisão estruturante)

Manual de serviço da GM e conteúdo de fórum são obras protegidas. **O repositório não redistribui
material de terceiros.** Fatos não são protegidos por direito autoral; a expressão é.

O que vai versionado no repo:

- `corpus/v1/manifest.yaml` — por documento: identificador, título, editora/autor, ano, URL ou
  procedência, `sha256` do arquivo original, tier, licença/estado de direitos.
- `corpus/v1/facts/` — fatos extraídos em formato estruturado, com ponteiro para documento e página.
- `corpus/v1/excerpts/` — trechos curtos com atribuição, dentro de uso justo, **apenas tier B**.
- `ingest/` — script determinístico que reconstrói o corpus a partir dos arquivos locais do usuário.

O que **não** vai: PDF de manual, digitalização de catálogo, thread de fórum copiada.

Quem clona aponta o pipeline para o próprio material. O `manifest` verifica hash e recusa arquivo
divergente.

### 6.2 Estrutura

Cada documento é normalizado para chunks com metadados:

```
chunk_id, doc_id, tier, page, section, kind ∈ {spec, procedure, prose}, text, jargon_terms[]
```

Chunking é **consciente de estrutura**: tabela de especificação é fatiada por linha (uma
especificação por chunk, nunca cortada no meio); procedimento é fatiado por passo; prosa por
parágrafo com sobreposição.

## 7. Arquitetura

```
                     ┌───────────────────────────────────────┐
  material local ──▶ │ ingest/  (Python, offline)            │
   (do usuário)      │  manifest → verificação de hash       │
                     │  extração → normalização de jargão    │
                     │  chunking consciente de estrutura     │
                     │  embedding (base | fine-tunado)       │
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
   │   Retriever  ◀interface▶  lexical │ dense │ hybrid(RRF)             │
   │   Reranker   ◀interface▶  none │ cohere-rerank                      │
   │   Generator  ◀interface▶  gemini │ (outro)                          │
   │   Tracer     ─ spans OTel por etapa                                 │
   └────────────────────────────────────┬────────────────────────────────┘
                                        ▼
                     ┌───────────────────────────────────────┐
                     │ web/  (build estático)                │
                     │  colunas comparativas · chunks+scores │
                     │  trace · aba Evals · re-rodar agora   │
                     └───────────────────────────────────────┘
```

### 7.1 Fronteiras

Cada interface é um módulo profundo: contrato pequeno, implementação substituível, testável em
isolamento com corpus sintético.

- **`Retriever`** — `retrieve(query, k, filters) -> list[Candidate]`. Implementações: `lexical`
  (`tsvector` + `pg_trgm`), `dense` (pgvector `<=>`), `hybrid` (RRF em SQL).
- **`Embedder`** — `embed(texts) -> matrix`. Implementações: `baseline` (modelo multilíngue pronto),
  `finetuned` (§10). Usado em ingestão e em consulta; **a mesma implementação nos dois lados** —
  divergência aqui é a fonte clássica de bug silencioso.
- **`Reranker`** — `rerank(query, candidates) -> list[Candidate]`. `none` e `cohere`.
- **`Generator`** — `generate(query, context, contract) -> Answer`. Retorna afirmações com citação
  obrigatória e sinal explícito de abstenção.

### 7.2 O banco é artefato derivado

`corpus/v1/` é a fonte da verdade. O banco é construído dela por pipeline determinístico e **nada
escreve nele em runtime**. Log de consulta e traces vão para armazenamento separado. O serviço
valida `corpus_hash` no boot e recusa subir se divergir do commit.

Isso reconcilia "banco de verdade" com o driver nº 1: o banco é hasheável porque é reconstruível.

## 8. Modelo de dados

```sql
documents(doc_id pk, title, publisher, year, tier, source_ref, sha256, rights)
chunks(chunk_id pk, doc_id fk, tier, page, section, kind, text, tsv tsvector)
embeddings(chunk_id fk, model_key, vector vector(N), pk(chunk_id, model_key))
jargon(term pk, canonical, notes)
corpus_meta(corpus_hash, built_at, ingest_version)
```

`N` é a dimensão do embedder. Uma única coluna `vector(N)` só funciona porque o modelo fine-tunado
é derivado do baseline e **preserva a dimensão** — restrição que amarra a escolha do modelo base na
§11. Embedder de dimensão diferente exigiria tabela separada.

Dois embedders convivem na mesma tabela via `model_key` — trocar de embedder em runtime é um
`WHERE`, não um redeploy. Filtro de tier é `WHERE tier = ANY(...)`. Fusão híbrida é uma CTE com RRF.

## 9. Eixos de configuração

**Build-time** (custa índice):

- chunking — **um** no MVP
- embedder — `baseline` | `finetuned`

**Runtime** (livre, barato):

- estratégia — `lexical` | `dense` | `hybrid`
- reranker — `none` | `cohere`
- filtro de tier — `A` | `A+B`
- contrato de prompt — `citação obrigatória` | `livre` (o segundo existe só para demonstrar o
  contraste; nunca é padrão)

UI expõe **4 presets nomeados** na frente e um painel avançado para combinação manual.

## 10. Avaliação

### 10.1 Duas camadas

| camada | mede | LLM? | quando |
|---|---|---|---|
| **Gate determinístico** | `recall@k`, MRR, nDCG — só recuperação | não | todo commit, CI, segundos, custo zero |
| **Avaliação de geração** | groundedness, precisão de citação, abstenção correta | sim (juiz) | sob demanda, local; saída commitada |

O gate quebra o build em regressão de recuperação. É onde vive o argumento do fine-tune, e é
totalmente determinístico — não precisa de chamada de API.

### 10.2 Conjuntos

- **`eval/facts.jsonl`** — pergunta → valor exato + `chunk_id` correto. Métrica: match numérico com
  tolerância declarada + `recall@k`.
- **`eval/recipes.jsonl`** — pergunta aberta → rubrica. Métricas: groundedness, precisão de citação,
  abstenção correta.
- **`eval/abstention.jsonl`** — perguntas deliberadamente **fora** do corpus. Acerto = recusar.

### 10.3 Não-determinismo é reportado, não escondido

Nunca publicar valor pontual da camada estocástica. Cada pergunta roda `k` vezes; a UI mostra média
e dispersão. Todo resultado é um **registro de corrida**:

```
run_id, git_sha, corpus_hash, config, model_id, temperature, judge_model,
prompt_version, n, started_at, metrics{...}, per_item[...]
```

Gerado por execução, **nunca escrito à mão**. Qualquer número na UI que não tenha rastro até um
registro de corrida é bug.

CI acumula corridas → série histórica por métrica. Mudança de modelo do provedor aparece como
degrau no gráfico — e isso é conteúdo.

### 10.4 Juiz

- Juiz é **cross-family** em relação ao gerador (gerador Gemini, juiz Claude). Reduz viés de
  auto-preferência. Declarado no README.
- **Juiz calibrado ou não conta.** ~20 itens rotulados à mão pelo autor; publica-se a taxa de
  concordância juiz↔humano. Sem esse número, a métrica de geração não é reportada.
- Escolha de rodar o juiz localmente em vez de no CI é **decisão de custo consciente** — vira ADR
  e é documentada como tal.

### 10.5 Contaminação

Regra inegociável: **conjuntos de avaliação são escritos por humano e nunca gerados pelo mesmo LLM
que gera dados de treino.** Documentado no README e verificado por procedência no arquivo.

## 11. Fine-tuning do embedder

Fine-tune do **embedder**, nunca do gerador.

- **Positivos:** sintéticos em massa — LLM lê cada chunk e gera consultas que aquele chunk responde.
- **Negativos difíceis:** minerados do próprio corpus — mesma especificação em ano, motor ou versão
  vizinhos (GSi 2.0 vs SR 1.8; 93 vs 94). É daqui que vem o ganho.
- **Jargão:** pares `jargão → chunk canônico` a partir de `jargon`, curados à mão.
- **Treino:** fora da VM (GPU gratuita, Colab/Kaggle). Publica o modelo como artefato versionado.
- **Medição:** exclusivamente pelo gate determinístico, contra o conjunto humano.

**Se não melhorar, publica-se que não melhorou.** O resultado negativo é conteúdo válido e sustenta
a tese melhor que um sucesso não explicado.

## 12. Observabilidade

O trace **é** o produto. Cada consulta emite árvore de spans compatível com OpenTelemetry:

```
query
├── retrieve      (estratégia, k, candidatos, ms)
├── rerank        (provedor, entrada→saída, ms)
└── generate      (modelo, tokens in/out, custo estimado, ms)
```

Renderizado no painel do demo e gravado junto do registro de corrida. Nenhum backend de
observabilidade rodando na VM; formato OTel permite exportar para Jaeger local quando necessário.

## 13. Interface

- **Comparação** — N colunas (presets ou manual). Cada coluna: resposta com citação numerada, painel
  de chunks recuperados com score e tier, trace, tempo e custo.
- **Aba Evals** — dashboard por configuração: métricas com dispersão, série histórica, link para o
  registro de corrida bruto.
- **Botão "re-rodar agora"** — em qualquer pergunta do benchmark, executa ao vivo e mostra onde o
  resultado cai em relação à faixa registrada. Transforma o número publicado em hipótese falsificável.
- **Rótulo de tier** visível em toda citação: manual e post de fórum nunca se parecem na tela.
- Cada afirmação da UI linka para o write-up da decisão correspondente.

## 14. Infraestrutura

- **Execução:** Oracle Cloud, VM ARM (Ampere A1), Vinhedo/SP. Docker Compose: `serve` + `postgres`
  + proxy TLS.
- **Python fixado em 3.12** no container. O 3.14 local do autor não é suportado por
  `torch`/`sentence-transformers`.
- **CI:** GitHub Actions — testes, gate determinístico, build de imagem multi-arch.
- **Modelos:** Gemini (free tier) como gerador; Cohere rerank (free tier) como reranker; embedder
  local (CPU) na VM.
- **Resiliência de quota:** resultados do benchmark são pré-computados e servidos estaticamente; se
  o free tier esgotar, o caminho curado continua de pé. Consulta livre tem rate limit e cache por
  hash de pergunta.
- **Reranker é dependência externa** — resultados de rerank usados em avaliação são cacheados e
  gravados no registro de corrida, para que a medição não dependa da disponibilidade do serviço.

## 15. Fases

Cada fase é uma fatia vertical publicável, com um write-up em inglês ao final.

**Fase 0 — Spike de corpus (gate do projeto).**
Reunir material tier A e escrever **20 perguntas de fato com resposta exata e fonte**. Sem código.
Falhou → muda o recorte do corpus antes de investir em arquitetura.

**Fase 1 — MVP: baseline honesto.**
Ingestão tier A → Postgres/pgvector → `lexical` vs `dense` → gate determinístico no CI → UI de duas
colunas com traces → deploy público.
*Write-up: "Building a reproducible retrieval benchmark for a corpus nobody has."*

**Fase 2 — Híbrido e reranking.**
RRF em SQL, reranker Cohere, terceira e quarta colunas, painel avançado.
*Write-up: "What reranking actually bought me — in nDCG and in milliseconds."*

**Fase 3 — Tier B e contrato de citação.**
Ingestão de comunidade com atribuição, filtro de tier, `recipes.jsonl`, juiz calibrado, conjunto de
abstenção.
*Write-up: "Grading a RAG system on knowing when to shut up."*

**Fase 4 — Fine-tune do embedder.**
Geração de pares, mineração de negativos difíceis, treino fora da VM, segundo `model_key`, coluna
comparativa, ganho medido pelo gate.
*Write-up: "Fine-tuning an embedder on garage slang: the numbers."*

**Fase 5 — Série histórica e fechamento.**
Dashboard histórico, README consolidado, índice de ADRs, post de retrospectiva.

## 16. Convenções

- **Idioma:** código, README, ADRs e write-ups em **inglês**. Corpus, jargão e perguntas de
  avaliação em **PT-BR** — o vocabulário é o objeto de estudo, não ruído.
- **Licenças:** código MIT. Datasets derivados sob licença aberta declarada. Material de terceiros
  não é redistribuído (§6.1).
- **ADRs previstos:** banco como artefato derivado · não-redistribuição de corpus · avaliação em
  duas camadas · juiz local em vez de CI · eixos build-time vs runtime · reranker hospedado como
  dependência externa · latência como observável, não SLO.

## 17. Riscos

| risco | impacto | mitigação |
|---|---|---|
| Material tier A insuficiente | mata o projeto | Fase 0 é gate explícito |
| Fine-tune não melhora | perde o clímax | resultado negativo é write-up válido |
| Free tier muda ou some | demo degrada | benchmark pré-computado + rate limit + cache |
| Escopo escapa para "mais carros" | nunca publica | um veículo é decisão travada até a Fase 5 |
| Juiz sem calibração | métrica sem valor | métrica de geração não é publicada sem concordância medida |
| ARM sem wheel de ML | bloqueia serving | embedder pequeno em CPU; treino fora da VM |

## 18. Decisões assumidas para revisão

Escolhidas por julgamento, não discutidas explicitamente. Corrigir na revisão se discordar:

1. Nome **Garage** — decidido, alinhado ao repositório `G59-Toneli/garage`.
2. Repositório e write-ups em inglês; corpus em PT-BR.
3. Licença MIT para código.
4. GitHub Actions como CI.
5. Conjunto de abstenção (`abstention.jsonl`) como terceiro conjunto de avaliação — não foi discutido
   antes, mas sustenta a métrica mais forte do projeto.
6. Um único chunking no MVP, com chunking como segundo eixo de build adiado indefinidamente.
