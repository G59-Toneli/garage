// The re-run button: execute a published number live and show where the result falls.
//
// ADR-0004 says a published number is a claim a visitor may try to falsify rather than a figure to
// trust. Everything else on this site makes that *inspectable* — the trace, the chunk list, the
// committed record. This is the one control that makes it *actionable*: press it and the same
// question goes to the same provider under the same configuration, right now, from this machine.
//
// ## What it can honestly promise, and what it must refuse to
//
// The whole design of this file is a set of refusals, because the obvious version of this feature —
// "your run was 8% faster and produced a better answer" — is three separate lies.
//
//   **Retrieval order.** A strong comparison, and the only one here that is. Retrieval is
//   deterministic over a fixed artifact, so the ranking must match. When it does not, that is shown
//   as a finding, in full, with both lists — never hidden, never smoothed. ADR-0008 measured zero
//   order differences in the top ten between x86-64 and aarch64 and did so with a margin of 1.13×
//   against the analytical bound, which is thin. If this ever fires, the note beside it says what it
//   most likely is: floating-point arithmetic between architectures, not a retrieval bug.
//
//   **Generated quantities.** One draw, on aarch64, against n draws recorded on x86-64. The
//   provider's own variance dwarfs anything architecture could contribute, so the only statement
//   available is "inside" or "outside the observed range", with the n printed beside it. There is no
//   "better", no "worse" and no percentage difference anywhere in this file, and a reviewer should
//   treat the appearance of one as a defect rather than an improvement.
//
//   **Latency.** Not compared at all, and the absence is stated rather than silent. ARM against x86,
//   cold connection, one sample — the waterfall on this very page is already labelled "não é uma
//   medição de desempenho", and offering the confrontation would take that back. `generate_ms` is
//   excluded by `adapt.COMPARABLE_METRICS`.
//
// ## Why the marks are the same strip plot
//
// Reused from `showcasescreen.spreadRow`'s vocabulary rather than reinvented: n recorded marks plus
// the live one, distinct in shape and in label, on one axis widened to contain it. **No error bar,
// no mean, no standard deviation** — the same rule, for the same reason, and if anything the reason
// is stronger here, because a reader looking at their own result is the reader most likely to want
// a verdict the data cannot give.

import { el, clear, number, usd, EM_DASH } from "./dom.js";
import { compareRetrievalOrder, compareToSpread, liveSampleMetrics } from "./adapt.js";

// Printed under every re-run, always, in this exact wording. It is the caveat that makes the panel
// above it readable, so it is not conditional, not collapsible and not shortened.
const FOOTNOTE =
  "1 amostra ao vivo em aarch64 contra n amostras registradas em x86-64, mesmo corpus_hash, mesmo " +
  "modelo. A recuperação é determinística e a ordem deve coincidir (ADR-0008). A geração é " +
  "estocástica: uma amostra fora do intervalo não é uma regressão, é uma amostra.";

export function attachRerun(region, view, item) {
  for (const arm of view.arms) {
    if (arm.failed || !arm.spread) continue;
    const column = region.querySelector(`.arm[data-strategy="${cssEscape(arm.strategy)}"]`);
    if (!column) continue;
    column.append(rerunPanel(arm, item, view.showcase));
  }
}

function rerunPanel(arm, item, showcase) {
  const results = el("div", { class: "rerun-results" });
  const button = el("button", {
    class: "rerun-button",
    text: "Re-executar esta pergunta ao vivo",
    attrs: { type: "button" },
  });
  const note = el("p", { class: "aside", attrs: { role: "status", "aria-live": "polite" } });

  button.addEventListener("click", () => {
    button.disabled = true;
    // "executando", not "chamando o provedor". The old wording asserted a model call before the
    // response could say whether one would happen — and on a build with no generator it was simply
    // false, printed a second before a panel that (then) implied the call had succeeded. What is
    // certainly true is that a request is going out and that it *may* cost a generation; the
    // outcome below says which of the two happened.
    note.textContent =
      "executando a pergunta ao vivo agora — se este build tiver gerador, isto gasta uma geração " +
      "do orçamento diário do site…";
    clear(results);
    runLive(arm, item)
      .then((body) => {
        clear(results);
        results.append(...outcome(body, arm, showcase));
        note.textContent = "";
      })
      .catch((failure) => {
        // A failed re-run is reported in place and takes nothing else down. The recorded numbers
        // beside it are still valid and are still the thing the visitor came for.
        clear(results);
        note.textContent = `a re-execução não completou: ${failure.message}`;
      })
      .finally(() => {
        button.disabled = false;
      });
  });

  return el("article", { class: "panel rerun" }, [
    el("h4", { text: "Falsifique este número" }),
    el("p", {
      class: "aside",
      text:
        "Este botão executa a mesma pergunta, na mesma configuração, contra o mesmo provedor, " +
        "agora, a partir desta máquina. O resultado aparece como uma marca distinta sobre as " +
        "amostras registradas. Nada aqui diz 'melhor' ou 'pior' — veja a nota no rodapé.",
    }),
    button,
    note,
    results,
    el("p", { class: "aside footnote", text: FOOTNOTE }),
  ]);
}

async function runLive(arm, item) {
  const response = await fetch("/query", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      question: item.question,
      strategy: arm.strategy,
      k: arm.k,
      tiers: arm.tiers,
      contract: arm.contract,
      // The flag that makes this a re-run rather than a lookup. Without it the endpoint would
      // recognise a curated question and hand back the very record we are trying to test.
      rerun: true,
    }),
  });
  if (!response.ok) {
    const detail = await response.json().catch(() => null);
    throw new Error(`HTTP ${response.status}${detail && detail.detail ? ` — ${detail.detail}` : ""}`);
  }
  return response.json();
}

function outcome(body, arm, showcase) {
  if (body.origin === "precomputed" || (body.origin_detail && body.origin_detail.rerun_refused)) {
    // The budget refused the live call. Said plainly, because a page that silently redisplayed the
    // same recorded figures would look exactly like a successful re-run that happened to match.
    return [
      el("div", { class: "rerun-refused" }, [
        el("strong", { text: "A re-execução ao vivo não aconteceu." }),
        el("p", {
          text:
            "O orçamento diário de geração deste site acabou, então nenhuma chamada foi feita e " +
            "nada abaixo mudou. Os números continuam sendo os gravados. O orçamento reinicia à " +
            "meia-noite UTC.",
        }),
      ]),
    ];
  }

  const order = compareRetrievalOrder(
    arm.chunks.map((chunk) => chunk.chunkId),
    (body.chunks || []).map((chunk) => chunk.chunk_id)
  );

  // Half of this button is retrieval and half of it is generation, and the two halves fail
  // independently. Retrieval always ran — it is local and free — so `orderBlock` is always drawn.
  // Generation may not have happened at all, and when it did not, saying nothing is the one thing
  // this panel must not do.
  //
  // This is a real defect that shipped and was caught in review, on the screen that exists to
  // support the project's central claim. On a deployment with no `GARAGE_GEMINI_API_KEY` — which
  // the production overlay does **not** require — the endpoint answers `origin: "live"` with
  // `answer: null` and `rerun_refused: false`. The old code fell straight through to the success
  // branch, `liveSampleMetrics` returned null, `rows` came back empty, and a button labelled
  // "Falsifique este número" reported "a ordem recuperada é idêntica à registrada" and nothing
  // else — immediately after a status line promising the provider was being called. A visitor read
  // a successful re-run of a generation that never occurred.
  //
  // Three ways the generated half can be absent, and they are different facts, so they get
  // different sentences. The same discipline as the five answer states: an absence travels as an
  // absence, and is never a silently shorter panel.
  const absence = generationAbsence(body);
  if (absence) return [orderBlock(order), absence, latencyBlock()];

  const rows = compareToSpread(arm.spread, liveSampleMetrics(body));
  return [orderBlock(order), ...(rows.length ? [metricsBlock(rows, arm)] : []), latencyBlock()];
}

// Null when a model genuinely answered and its numbers may be plotted. Otherwise the block that says
// which half did not happen.
//
// The three conditions are tested in this order and none of them may be folded together. `answer ===
// null` is a build with no generator configured — nothing failed, and that is a supported
// configuration this service has always had. `degraded` is a provider that was asked and did not
// answer. `provider === null` on an answer that exists is the zero-cost abstention: the retriever
// came back empty, so `app._answer` abstained without asking anybody. All three would otherwise
// reach `liveSampleMetrics`, which would dutifully report zero tokens and zero cost and plot them
// against the recorded spread as though they were a measurement.
function generationAbsence(body) {
  const answer = body.answer;
  if (answer === null || answer === undefined) {
    return absenceBlock(
      "Nenhum modelo foi chamado: este build não tem gerador configurado.",
      "A metade geradora desta comparação não aconteceu, então não há tokens, custo nem citações " +
        "para colocar sobre as amostras registradas. A comparação de ordem acima é real e é de " +
        "agora — ela não depende de provedor nenhum. Para falsificar também a resposta gerada, " +
        "este serviço precisa de uma chave de provedor."
    );
  }
  if (answer.degraded) {
    return absenceBlock(
      "O provedor foi chamado e não respondeu.",
      "Nada foi gerado, então não há amostra ao vivo para posicionar sobre as registradas. A " +
        "comparação de ordem acima continua válida. O motivo cru está no painel de resposta desta " +
        "coluna."
    );
  }
  if (answer.provider === null || answer.provider === undefined) {
    return absenceBlock(
      "Abstenção de custo zero: nenhum modelo foi chamado.",
      "A recuperação não trouxe nada, então a geração se absteve sem perguntar a ninguém — o " +
        "comportamento mais barato e correto que este sistema tem. Não há amostra ao vivo, e um " +
        "zero aqui seria uma medição inventada de uma chamada que não existiu."
    );
  }
  return null;
}

function absenceBlock(heading, body) {
  // Neutral, not an error frame. In all three cases nothing failed on this page, and the strong
  // comparison above it succeeded.
  return el("div", { class: "rerun-absent" }, [
    el("strong", { text: heading }),
    el("p", { text: body }),
  ]);
}

function orderBlock(order) {
  if (order.identical) {
    return el("div", { class: "rerun-order rerun-order-match" }, [
      el("strong", { text: "A ordem recuperada é idêntica à registrada." }),
      el("p", {
        class: "aside",
        text:
          `${order.recorded.length} trechos, na mesma sequência. Esta é a única comparação forte ` +
          `desta tela: a recuperação é determinística sobre um artefato fixo, então ela tinha que ` +
          `coincidir — e coincidiu.`,
      }),
    ]);
  }
  // Not an error and not hidden. It is the finding, and it is displayed as one.
  return el("div", { class: "rerun-order rerun-order-differs" }, [
    el("strong", { text: "Achado: a ordem recuperada não é a registrada." }),
    el("p", {
      text: order.sameMembership
        ? "Os mesmos trechos voltaram, em sequência diferente."
        : "O conjunto de trechos recuperados é diferente.",
    }),
    el("p", {
      class: "aside",
      text:
        "A causa mais provável é aritmética de ponto flutuante entre arquiteturas — este serviço " +
        "roda em aarch64 e o registro foi medido em x86-64 — e não um defeito da recuperação. O " +
        "ADR-0008 mediu zero diferenças de ordem no top-10 entre as duas, com margem de 1,13× " +
        "contra o limite analítico, o que é estreito. As duas listas estão abaixo, na íntegra.",
    }),
    el("div", { class: "rerun-lists" }, [
      el("div", {}, [
        el("h5", { text: "registrada" }),
        el("ol", { class: "mono small" }, order.recorded.map((id) => el("li", { text: id }))),
      ]),
      el("div", {}, [
        el("h5", { text: "ao vivo" }),
        el("ol", { class: "mono small" }, order.live.map((id) => el("li", { text: id }))),
      ]),
    ]),
  ]);
}

function metricsBlock(rows, arm) {
  return el("div", { class: "rerun-metrics" }, [
    el("h5", { text: "Sua amostra sobre as registradas" }),
    el("div", { class: "spreads" }, rows.map((row) => metricRow(row, arm))),
  ]);
}

function metricRow(row, arm) {
  const format = row.key === "cost_usd" ? (value) => usd(value) : (value) => number(value, 0);
  const track = el("div", { class: "strip" });
  for (const mark of row.marks) {
    if (mark.fraction === null) continue;
    track.append(
      el("span", {
        class: "strip-mark",
        style: { left: mark.fraction * 100 },
        attrs: {
          tabindex: "0",
          role: "listitem",
          "aria-label": `amostra registrada ${mark.index + 1}: ${mark.value}`,
          title: `amostra registrada ${mark.index + 1}: ${mark.value}`,
        },
      })
    );
  }
  if (row.liveFraction !== null) {
    // Distinct in shape as well as in colour, and announced as "sua execução". A reader who cannot
    // see the difference in tone must still be able to find their own mark.
    track.append(
      el("span", {
        class: "strip-mark strip-live",
        style: { left: row.liveFraction * 100 },
        attrs: {
          tabindex: "0",
          role: "listitem",
          "aria-label": `sua execução ao vivo: ${row.liveValue}`,
          title: `sua execução ao vivo: ${row.liveValue}`,
        },
      })
    );
  }
  track.setAttribute("role", "list");
  track.setAttribute("aria-label", `${row.n} amostras registradas de ${row.label}, mais a sua`);

  return el("div", { class: "spread-row" }, [
    el("div", { class: "spread-head" }, [
      el("span", { class: "spread-label", text: row.label }),
      el("span", {
        class: "spread-range",
        // The verdict, and the exhaustive list of verdicts this panel is allowed to reach. Inside,
        // outside, or not comparable. `n` travels with every one of them.
        text:
          row.inside === null
            ? `sua execução: ${format(row.liveValue)} · não comparável (n=${row.n})`
            : row.inside
              ? `sua execução: ${format(row.liveValue)} · dentro do intervalo observado em n=${row.n} ` +
                `(${format(row.minimum)} — ${format(row.maximum)})`
              : `sua execução: ${format(row.liveValue)} · fora do intervalo observado em n=${row.n} ` +
                `(${format(row.minimum)} — ${format(row.maximum)})`,
      }),
    ]),
    track,
  ]);
}

function latencyBlock() {
  // An absence, stated. Leaving latency out silently would let a reader assume it was compared and
  // found equal, which is a stronger claim than "we refuse to make this comparison".
  return el("p", {
    class: "aside rerun-latency",
    text:
      "Latência não é comparada, de propósito. Uma amostra em aarch64 com conexão fria contra " +
      "amostras em x86-64 não sustenta nenhuma conclusão sobre desempenho, e a cascata de spans " +
      "desta página já é rotulada como 'não é uma medição de desempenho'.",
  });
}

function cssEscape(value) {
  return window.CSS && window.CSS.escape ? window.CSS.escape(value) : value.replace(/["\\]/g, "\\$&");
}
