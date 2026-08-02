// Components: view object in, DOM out. No state, no fetching, no reading of a server payload.
//
// Every function here takes the plain object `adapt.toView` produced and nothing else. That is what
// makes the eventual React port a rewrite of this file alone, and what makes issue #11 a new branch
// in the adapter with no change here at all.

import { el, clear, number, milliseconds, usd, superscript, EM_DASH } from "./dom.js";

export function renderComparison(view, root) {
  clear(root);

  if (!view.shared.agrees) {
    // Refusing to draw, on purpose. Two arms measured against different artifacts is not a
    // comparison, and a side-by-side that quietly spans two corpora would show a difference in
    // retrieval that is really a difference in the database. Run Record v2 makes this impossible by
    // construction, by holding `provenance` above the arms; two independent HTTP calls cannot, so
    // the check is done here.
    root.append(
      el("div", { class: "alert" }, [
        el("h2", { text: "Comparação recusada" }),
        el("p", {
          text:
            "As duas colunas responderam a partir de artefatos diferentes, então nada nelas é " +
            "comparável. corpus_hash recebidos:",
        }),
        el("ul", {}, view.shared.hashes.map((hash) => el("li", { class: "mono", text: hash }))),
      ])
    );
    return;
  }

  root.append(sharedHeader(view));
  if (view.origins) root.append(originBand(view.origins));
  if (view.overlap) root.append(overlapBand(view.overlap));
  if (view.floorNote) root.append(floorNote(view.floorNote));
  root.append(
    el(
      "div",
      { class: "columns" },
      view.arms.map((arm) => armColumn(arm, view.traceScaleMs))
    )
  );

  wireHighlighting(root);
}

// `question` and `corpus_hash` sit once, above both columns. They are properties of the comparison,
// not of an arm: printed per column they would look like two independent facts that happen to
// agree, when in fact the interface refuses to render at all unless they are the same fact.
function sharedHeader(view) {
  return el("section", { class: "shared" }, [
    el("h2", { class: "question", text: view.shared.question ?? "nenhuma coluna respondeu" }),
    el("dl", { class: "kv" }, [
      el("dt", { text: "corpus_hash" }),
      // Null only when every arm failed. It is left blank rather than filled with the hash of an
      // arm that never ran.
      el("dd", { class: "mono", text: view.shared.corpusHash ?? EM_DASH }),
    ]),
  ]);
}

// Where each column's answer came from, above the columns, on every comparison.
//
// A band and not a colour. "Live", "cached" and "precomputed" are three different claims about how
// much a number is worth, and a reader who is colour-blind, printing the page, or looking at a
// screenshot has to get all three — so each origin carries a word, a class *and* a `data-origin`
// attribute, and the word is never the only channel that changes.
//
// The sentences are as specific as the payload allows. "resposta em cache" alone invites the reader
// to assume it is minutes old; the time it was generated is the fact that makes the label useful.
function originBand(origins) {
  return el(
    "section",
    { class: "origins" },
    origins.map((arm) =>
      el("span", { class: `origin origin-${arm.origin ?? "unknown"}`, data: { origin: arm.origin ?? "" } }, [
        el("span", { class: "origin-strategy", text: arm.strategy }),
        el("span", { class: "origin-text", text: originSentence(arm) }),
      ])
    )
  );
}

function originSentence(arm) {
  if (arm.origin === "cache") {
    // The generation time, not the age, as the primary fact. An age in seconds is arithmetic the
    // reader has to redo against their own clock; a timestamp is a thing that happened.
    const when = arm.storedAt ? clockTime(arm.storedAt) : null;
    return when ? `resposta em cache, gerada às ${when}` : "resposta em cache";
  }
  if (arm.origin === "precomputed") {
    // Three facts, and the sample qualifier is one of them. `answer.tokens_out` on the panel below
    // is one draw of n, and printing it without saying so is exactly the failure ADR-0004 exists to
    // prevent — the showcase screen learned that in its own QA round.
    const draw =
      arm.displayedSample !== null && arm.sampleCount !== null
        ? ` · amostra ${arm.displayedSample + 1} de ${arm.sampleCount}`
        : "";
    const refused = arm.rerunRefused
      ? " · a re-execução ao vivo foi recusada pelo orçamento; este é o número gravado"
      : "";
    return `resposta pré-computada · showcase ${arm.showcaseId ?? EM_DASH}${draw}${refused}`;
  }
  if (arm.origin === "live_degraded") {
    return "geração não executada — o orçamento diário recusou a chamada; a recuperação abaixo é real e é de agora";
  }
  // A build with no generator is not a build with a full quota. It answers with retrieval alone,
  // which is a supported configuration and is stated as one — counting down a budget it can never
  // spend would be the band describing a different deployment.
  if (!arm.generationConfigured) return "resposta ao vivo · geração não configurada neste build";
  const left =
    arm.generationsRemaining === null
      ? ""
      : ` · ${arm.generationsRemaining} de ${arm.generationBudget} gerações restantes hoje`;
  return `resposta ao vivo${left}`;
}

function clockTime(iso) {
  // Local time, hours and minutes. Parsed defensively: this string comes off the wire and a page
  // that throws inside a status band would take the whole comparison down with it.
  const moment = new Date(iso);
  return Number.isNaN(moment.getTime())
    ? null
    : moment.toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" });
}

function overlapBand(overlap) {
  const parts = [
    ...overlap.perArm.map((arm) => `${arm.count} recuperados em ${arm.strategy}`),
    `${overlap.common} em comum`,
    ...overlap.only.map((arm) => `${arm.count} só em ${arm.strategy}`),
  ];
  return el("section", { class: "overlap" }, [
    el("span", { class: "overlap-label", text: "sobreposição" }),
    el("span", { text: parts.join(" · ") }),
  ]);
}

function floorNote(note) {
  return el("section", { class: "note" }, [
    el("strong", { text: "Uma coluna não recuperou nada. Isso é o comportamento projetado." }),
    el("p", {
      text:
        `A estratégia ${note.empty.join(", ")} descarta trechos abaixo de um piso de similaridade, ` +
        `então uma pergunta que o Corpus não cobre volta vazia — e a geração se abstém sem chamar ` +
        `modelo nenhum. A estratégia ${note.full.join(", ")} não tem piso: busca por vizinhos mais ` +
        `próximos sempre devolve os k mais próximos, por mais distantes que estejam. Compare os ` +
        `cossenos antes de ler a coluna cheia como "a que funcionou".`,
    }),
  ]);
}

// --- one column ----------------------------------------------------------------------------------

function armColumn(arm, traceScaleMs) {
  // A failed column reports its own failure and nothing else. Drawn as a *column*, in place, rather
  // than as a banner across the page: the other arm's chunks, answer and trace are still valid and
  // still the product, and a page-level error would throw them away. Issue #11 puts this behind a
  // quota, where one arm getting a 429 must not erase the other.
  if (arm.failed) {
    return el("section", { class: "arm", data: { strategy: arm.strategy } }, [
      el("header", { class: "arm-head" }, [el("h3", { text: arm.strategy })]),
      // `state-unreachable`, deliberately not `state-rejected`. The two are semantic opposites — a
      // rejection is a call that was answered and billed and that we refused, this is a call that
      // never completed — and giving them the same red frame made them indistinguishable side by
      // side. Neutral, dashed, closer to the "did not run" vocabulary than to the "was paid for" one.
      el("article", { class: "state state-unreachable" }, [
        el("h4", { text: "Esta coluna não respondeu" }),
        el("p", { text: arm.error }),
        el("p", {
          class: "aside",
          text: "A outra coluna continua válida. Nada aqui foi estimado nem preenchido.",
        }),
      ]),
    ]);
  }
  return el("section", { class: "arm", data: { strategy: arm.strategy } }, [
    el("header", { class: "arm-head" }, [
      el("h3", { text: arm.strategy }),
      el("dl", { class: "kv" }, [
        el("dt", { text: "embedder" }),
        // Null under lexical, and that null is the identity of the arm rather than missing data.
        el("dd", { class: "mono", text: arm.embedder ?? "sem índice denso" }),
        el("dt", { text: "k · tiers · contrato" }),
        el("dd", { text: `${arm.k} · ${arm.tiers.join(", ")} · ${arm.contract}` }),
        el("dt", { text: "traceId" }),
        el("dd", { class: "mono small", text: arm.trace ? arm.trace.traceId : EM_DASH }),
      ]),
    ]),
    answerBlock(arm),
    arm.answer.cost ? costBlock(arm.answer.cost) : null,
    arm.trace ? tracePanel(arm.trace, traceScaleMs) : null,
    chunkList(arm),
  ]);
}

// --- the five states, five components --------------------------------------------------------------

function answerBlock(arm) {
  const answer = arm.answer;
  if (answer.state === "disabled") return generationDisabled();
  if (answer.state === "abstained") return abstention(answer);
  if (answer.state === "degraded") return degradation(answer);
  if (answer.state === "rejected") return rejection(answer);
  return prose(answer, arm);
}

function generationDisabled() {
  // Neutral by design: no alert colour, no error glyph. Nothing failed — this build simply holds no
  // generator, and retrieval below is the whole product in that configuration.
  return el("article", { class: "state state-disabled" }, [
    el("h4", { text: "Geração não configurada neste build" }),
    el("p", { text: "Só recuperação. Nenhum modelo foi chamado, e nenhum estágio falhou." }),
  ]);
}

function abstention(answer) {
  // A correct refusal, labelled as correct. Presented as a failure it would be read as one, and the
  // abstention rate is the metric this project most wants a visitor to understand.
  return el("article", { class: "state state-abstained" }, [
    el("h4", { text: "Abstenção — comportamento correto" }),
    el("p", { text: answer.reason ?? "o Corpus não cobre esta pergunta" }),
    el("p", {
      class: "aside",
      text: answer.zeroCost
        ? "O Corpus não cobre isto. Nenhuma chamada ao modelo foi feita — custo zero."
        : "O Corpus não cobre isto. O modelo foi consultado e recusou responder.",
    }),
  ]);
}

function degradation(answer) {
  // One state, two headings, and the split is the whole of issue #11's refusal to add a sixth state.
  // "O provedor não respondeu" is false when the provider was never called — the day's budget said
  // no before anything left this machine — and a heading that is false is worse than a heading that
  // is vague. `cause` comes from `origin`, never from matching the Portuguese in `reason`.
  const budget = answer.cause === "budget";
  return el("article", { class: "state state-degraded", data: { cause: answer.cause ?? "provider" } }, [
    el("h4", {
      text: budget ? "A geração não foi executada — cota do dia" : "O provedor não respondeu",
    }),
    el("p", {
      text: budget
        ? "Nenhuma chamada foi feita. Este site tem um orçamento diário de gerações porque usa o " +
          "nível gratuito de um provedor pago, e ele acabou por hoje. As perguntas curadas do " +
          "showcase continuam idênticas, porque já foram pagas."
        : "Os trechos recuperados abaixo continuam válidos e continuam sendo o produto.",
    }),
    el("p", { class: "aside", text: answer.reason ?? "" }),
    // The provider's raw message is folded away rather than dropped: it is the one string here that
    // is useful to an operator and useless to a visitor.
    answer.detail
      ? el("details", {}, [
          el("summary", { text: "mensagem crua do provedor" }),
          el("pre", { class: "mono small", text: answer.detail }),
        ])
      : null,
  ]);
}

function rejection(answer) {
  return el("article", { class: "state state-rejected" }, [
    el("h4", { text: "Resposta recusada por nós" }),
    el("p", { text: "O gerador citou algo que não resolve, então a resposta não foi publicada." }),
    el("p", { class: "aside", text: answer.violation ?? "" }),
    el("p", {
      class: "aside",
      // The cost is shown anyway, immediately below, and this line says why: a configuration that
      // reliably breaks the citation contract must not appear as the cheap one.
      text: "O provedor respondeu e cobrou. O custo abaixo é real e está sendo mostrado de propósito.",
    }),
    answer.detail
      ? el("details", {}, [
          el("summary", { text: "citações recusadas, uma a uma" }),
          el("pre", { class: "mono small", text: answer.detail }),
        ])
      : null,
  ]);
}

function prose(answer, arm) {
  const paragraph = el("p", { class: "prose" });
  for (const [index, claim] of answer.claims.entries()) {
    if (index > 0) paragraph.append(" ");
    paragraph.append(claimNode(claim, arm));
  }
  return el("article", { class: "state state-answered" }, [
    el("h4", { text: "Resposta" }),
    answer.claims.length ? paragraph : el("p", { class: "prose", text: answer.text }),
  ]);
}

function claimNode(claim, arm) {
  const node = el("span", { class: claim.supported ? "claim" : "claim claim-unsupported" }, [
    // Kept on screen when unsupported, marked rather than deleted: a silently shorter answer hides
    // the failure, and the ADR-0004 judge is better served by a flagged sentence.
    claim.supported ? null : el("span", { class: "badge", text: "SEM SUPORTE" }),
    el("span", { text: claim.text }),
  ]);
  for (const citation of claim.citations) {
    node.append(citationButton(citation, arm));
  }
  return node;
}

function citationButton(citation, arm) {
  const chunk = arm.chunks.find((candidate) => candidate.chunkId === citation.chunkId);
  const tier = chunk ? chunk.tier : "";
  const label = chunk
    ? `citação ${citation.index}, Tier ${chunk.tier}, ${chunk.tierShort}, ${chunk.docTitle}, ` +
      (chunk.page === null ? "sem página" : `página ${chunk.page}`)
    : `citação ${citation.index}`;
  // A real `<button>`, not a styled span: it is operable from the keyboard, it is announced, and it
  // does something — the citation is only worth anything if a reader can get from it to the chunk.
  return el("button", {
    class: `cite tier-${tier.toLowerCase()}`,
    text: superscript(citation.index, tier),
    attrs: { type: "button", "aria-label": label },
    data: { chunk: citation.chunkId },
    on: {
      click(event) {
        const target = event.currentTarget
          .closest(".arm")
          .querySelector(`.chunk[data-chunk="${cssEscape(citation.chunkId)}"]`);
        if (!target) return;
        target.scrollIntoView({ block: "center", behavior: "smooth" });
        target.classList.add("flash");
        setTimeout(() => target.classList.remove("flash"), 1200);
      },
    },
  });
}

// --- cost ------------------------------------------------------------------------------------------

function costBlock(cost) {
  // The cost cell carries two facts in one place, deliberately: the figure — a dash when null, and
  // null means "no published price" or "no call", never zero — and the date of the price table it
  // came from. A cost with no date is a number asking to be believed.
  const priced = cost.costUsd !== null && cost.costUsd !== undefined;
  const costCell = el("dd", {}, [
    usd(cost.costUsd),
    priced
      ? el("span", {
          class: "aside",
          text: ` ${cost.estimated ? "estimado" : "medido"}, tabela de ${cost.pricingAsOf ?? "data desconhecida"}`,
        })
      : null,
  ]);
  return el("article", { class: "panel" }, [
    el("h4", { text: "Custo e contrato" }),
    el("dl", { class: "kv" }, [
      el("dt", { text: "provedor · modelo" }),
      // Both null on the zero-cost abstention. Writing a provider name here would be inventing a
      // call that never happened.
      el("dd", { text: cost.provider ? `${cost.provider} · ${cost.model ?? EM_DASH}` : "nenhuma chamada" }),
      el("dt", { text: "tokens entrada / saída" }),
      el("dd", { text: `${cost.tokensIn} / ${cost.tokensOut}` }),
      el("dt", { text: "custo estimado" }),
      costCell,
      // The contract health panel is printed even when every figure is zero. Zeros here are the
      // result — "no citation was invalid" — and hiding them would leave a reader unable to tell a
      // clean run from a run where nobody checked.
      el("dt", { text: "citações inválidas" }),
      el("dd", { text: cost.invalidCitations }),
      el("dt", { text: "afirmações sem suporte" }),
      el("dd", { text: cost.unsupportedClaims }),
      el("dt", { text: "estado contraditório" }),
      el("dd", { text: cost.contradictory ? "sim" : "não" }),
    ]),
  ]);
}

// --- the trace, rendered ---------------------------------------------------------------------------

function tracePanel(trace, scaleMs) {
  const rows = trace.rows.map((row) => traceRow(row, scaleMs));
  return el("article", { class: "panel" }, [
    el("h4", { text: "Árvore de spans" }),
    el("p", {
      class: "aside",
      // Both labels are the honest version of what the numbers are. The shared scale is stated so
      // the two columns can be read against each other; the wall-clock caveat is stated so nobody
      // reads a latency comparison out of a demo that is not measuring latency.
      text:
        `escala compartilhada, 0–${milliseconds(scaleMs)} · ` +
        `tempo de parede, uma amostra, conexão fria — não é uma medição de desempenho`,
    }),
    el("div", { class: "waterfall" }, rows),
  ]);
}

function traceRow(row, scaleMs) {
  const width = row.ran && row.durationMs !== null ? (row.durationMs / scaleMs) * 100 : 0;
  const offset = row.ran && row.offsetMs !== null ? (row.offsetMs / scaleMs) * 100 : 0;
  const bar = el("div", { class: "bar-track" }, [
    row.ran
      ? el("div", {
          class: row.error ? "bar bar-error" : "bar",
          // `min-width` in CSS keeps a sub-millisecond stage visible; the number to its right is the
          // datum, and the bar is only support.
          style: { "margin-left": offset, width },
        })
      : el("div", { class: "bar bar-absent" }),
  ]);
  return el("div", { class: `trace-row depth-${row.depth} ${row.ran ? "" : "not-run"}` }, [
    el("div", { class: "trace-head" }, [
      el("span", { class: "trace-name", text: row.name }),
      row.error ? el("span", { class: "badge badge-error", text: "ERRO" }) : null,
      // Absent stages read "não executado" with an em dash for a duration. Never `0 ms`: that would
      // be the trace describing a pipeline it does not have.
      el("span", { class: "trace-time", text: row.ran ? milliseconds(row.durationMs) : `${EM_DASH} não executado` }),
    ]),
    bar,
    row.attributes.length
      ? el("details", { class: "attrs" }, [
          el("summary", { text: `${row.attributes.length} atributos` }),
          el(
            "dl",
            { class: "kv" },
            row.attributes.flatMap((attribute) => [
              el("dt", { class: "mono small", text: attribute.key }),
              el("dd", { class: "mono small", text: attribute.value === null ? EM_DASH : String(attribute.value) }),
            ])
          ),
        ])
      : null,
  ]);
}

// --- chunks ------------------------------------------------------------------------------------------

function chunkList(arm) {
  if (!arm.chunks.length) {
    return el("article", { class: "panel" }, [
      el("h4", { text: "Trechos recuperados" }),
      el("p", { class: "aside", text: "Nenhum. A estratégia não encontrou nada acima do seu piso." }),
    ]);
  }
  return el("article", { class: "panel" }, [
    el("h4", { text: `Trechos recuperados (${arm.chunks.length})` }),
    el("p", {
      class: "aside",
      // The unit is named, always. An RRF score around 0.016 and a cosine around 0.9 are not on the
      // same scale and are never drawn on one; saying so is cheaper than a reader discovering it.
      text: `score (${arm.scoring.unit ?? "sem unidade declarada"}) · barra ${arm.scoring.axis}`,
    }),
    el("ol", { class: "chunks" }, arm.chunks.map((chunk) => chunkCard(chunk, arm))),
  ]);
}

function chunkCard(chunk, arm) {
  // Four redundant channels for the tier, because colour alone fails WCAG 1.4.1 and the difference
  // between a factory manual and someone's memory of one is the difference this whole project is
  // about (design §13). The class carries the border shape (solid vs dashed) and the typography
  // (semibold vs italic); the label below is the text channel; colour is the fourth and last.
  return el("li", { class: `chunk tier-${chunk.tier.toLowerCase()}`, data: { chunk: chunk.chunkId }, attrs: { tabindex: "0" } }, [
    el("div", { class: "chunk-head" }, [
      el("span", { class: "position", text: `#${chunk.position}` }),
      el("span", { class: "tier-label", text: chunk.tierLabel }),
      el("span", { class: "score", text: `${number(chunk.score, 6)} ${arm.scoring.unit ?? ""}`.trim() }),
    ]),
    scoreBar(chunk, arm.scoring),
    el("div", { class: "chunk-source" }, [
      el("span", { text: chunk.docTitle }),
      el("span", { class: "sep", text: " · " }),
      // Null page is legitimate and says so. `p. null` would be the interface reporting a bug that
      // is not there.
      el("span", { text: chunk.page === null ? "sem página" : `p. ${chunk.page}` }),
      chunk.section ? el("span", { class: "sep", text: " · " }) : null,
      chunk.section ? el("span", { text: chunk.section }) : null,
      el("span", { class: "sep", text: " · " }),
      el("span", { class: "mono small", text: chunk.chunkId }),
    ]),
    chunkText(chunk),
    componentTable(chunk),
    crossReference(chunk),
  ]);
}

function chunkText(chunk) {
  if (!chunk.textAbsent) return el("p", { class: "chunk-text", text: chunk.text });
  // The one component this file gained for the showcase, and it is the vocabulary this interface
  // already speaks: an absence travels as an absence. A showcase record stores `chunk_id`s and never
  // the material (ADR-0003), so a clone without the operator's Corpus lands here — with the rank,
  // the score, the tier, the document and the identifier all intact and only the paragraph missing.
  //
  // Deliberately not an empty `<p>`, which is what a null text rendered as a string produces: a
  // blank card is an absence pretending to be a short chunk. And deliberately not an error: nothing
  // failed, and everything around it is still the product.
  return el("p", { class: "chunk-text chunk-text-absent" }, [
    el("strong", { text: "Texto não disponível neste artefato." }),
    el("span", {
      text:
        " O registro guarda o identificador do trecho e nunca as palavras dele, e esta base não " +
        "tem o material do operador para hidratá-lo. O trecho está identificado acima.",
    }),
  ]);
}

function scoreBar(chunk, scoring) {
  return el("div", { class: "score-track" }, [
    // Gridlines only where the axis is absolute. On a cosine axis they are what makes "the nearest
    // neighbour was still 0.31 away" legible without inventing a floor the retriever does not have.
    ...scoring.ticks.map((tick) =>
      el("div", { class: "tick", style: { left: tick * 100 }, data: { tick: String(tick) } })
    ),
    el("div", { class: "score-fill", style: { width: chunk.scoreFraction * 100 } }),
  ]);
}

function componentTable(chunk) {
  return el(
    "dl",
    { class: "kv components" },
    // Iterated generically over whatever keys arrived, so a third signal shows up on its own.
    chunk.components.flatMap((component) => [
      el("dt", { text: component.label }),
      el("dd", {
        class: component.fired ? "" : "did-not-fire",
        // A null rank is "this signal did not fire", which is the "matched on trigram alone" case —
        // the most interesting row in the panel, and the one a `0` would erase.
        text: component.fired
          ? component.isRank
            ? `rank ${number(component.value, 0)}`
            : number(component.value, 6)
          : "não disparou",
      }),
    ])
  );
}

function crossReference(chunk) {
  if (chunk.onlyHere) {
    return el("p", { class: "cross", text: `#${chunk.position} · só nesta coluna` });
  }
  const where = chunk.alsoIn.map((other) => `${other.strategy} (#${other.position})`).join(", ");
  // Position pairs in text, never a forced vertical alignment. Aligning the two columns row by row
  // would require reordering one of them, and the order *is* the information each column exists to
  // show. Scroll is independent for the same reason: a column that abstained beside one with ten
  // chunks would be all whitespace.
  return el("p", { class: "cross", text: `#${chunk.position} · também em ${where}` });
}

// --- cross-column highlighting -----------------------------------------------------------------------

function wireHighlighting(root) {
  const indicator = el("div", { class: "peek", attrs: { hidden: true } });
  root.append(indicator);

  const cards = root.querySelectorAll(".chunk");
  for (const card of cards) {
    const id = card.dataset.chunk;
    const highlight = (on) => {
      for (const twin of root.querySelectorAll(`.chunk[data-chunk="${cssEscape(id)}"]`)) {
        twin.classList.toggle("linked", on);
      }
      if (!on) {
        indicator.hidden = true;
        return;
      }
      // The highlight is synchronised, the scroll deliberately is not. When the twin is off screen a
      // text indicator says where it is, which costs one line and no empty space.
      const twins = [...root.querySelectorAll(`.chunk[data-chunk="${cssEscape(id)}"]`)].filter(
        (node) => node !== card
      );
      const hidden = twins.filter((node) => !inView(node));
      if (!hidden.length) {
        indicator.hidden = true;
        return;
      }
      const twin = hidden[0];
      const arrow = twin.getBoundingClientRect().top < 0 ? "↑" : "↓";
      indicator.textContent = `também na outra coluna, posição ${twin.querySelector(".position").textContent} ${arrow}`;
      indicator.hidden = false;
    };
    card.addEventListener("mouseenter", () => highlight(true));
    card.addEventListener("mouseleave", () => highlight(false));
    card.addEventListener("focus", () => highlight(true));
    card.addEventListener("blur", () => highlight(false));
  }
}

function inView(node) {
  const box = node.getBoundingClientRect();
  return box.bottom > 0 && box.top < window.innerHeight;
}

function cssEscape(value) {
  return window.CSS && window.CSS.escape ? window.CSS.escape(value) : value.replace(/["\\]/g, "\\$&");
}
