// The precomputed screen: curated questions rendered with **zero model calls**.
//
// Three reads, all of them free and local: `GET /showcase` for the record list, `GET /showcase/{id}`
// for the bytes on disk, and `GET /chunks?ids=...` to hydrate the paragraphs the record deliberately
// does not carry (ADR-0003, `docs/showcase.md`). There is no `POST /query` in this file and there
// must never be one. If a fourth `fetch` ever appears here, check what it costs before merging it.
//
// The comparison itself is drawn by `renderComparison`, unchanged, from a view produced by
// `toView({source:"showcase"})`. That is the whole payoff of the adaptation boundary: a stored
// record and two live HTTP calls reach the same components through the same shape. What this file
// adds around that is everything specific to the numbers being *old*.
//
// ## Saying "precomputed" in a way that survives
//
// A visitor who reads these as live numbers has been misled, so the claim is carried on four
// channels, deliberately mirroring how the tier is carried on four (`docs/ui.md`):
//
//   1. **Text** — a banner naming the `showcase_id`, the date the samples were drawn, the provider,
//      the model, the temperature and n.
//   2. **A stamp beside every panel of numbers.** Not beside every individual numeral, which would
//      be unreadable; beside every *group* of them — the answer, the cost, the waterfall, the chunk
//      list, each spread. Every one carries the `showcase_id` itself, so a screenshot of any single
//      panel still says which record it came from.
//   3. **Texture, not tone.** A repeating diagonal hatch behind the region. Colour alone fails in
//      monochrome and fails for a colour-blind reader, and this is the same argument the tier
//      treatment makes; a lighter shade of grey would say nothing at all in print.
//   4. **`aria-label`** on each stamped region, spelling out what it is for a reader who gets none
//      of the first three.
//
// ## The strip plot
//
// n draws are drawn as n marks on a line, and that is all. **No error bar, no mean, no standard
// deviation, no box.** Ten samples do not support a sigma, and the record holds no scalar for any
// stochastic quantity precisely so that this file *cannot* render one (ADR-0004). The two numbers
// printed beside the marks are the minimum and the maximum, which were observed, and a count of how
// many distinct values appeared — which at temperature 0 is frequently 1, and "this did not move at
// all" is the most useful thing the panel says.

import { el, clear, number, usd, EM_DASH } from "./dom.js";
import { toView } from "./adapt.js";
import { renderComparison } from "./render.js";
import { mountProvenanceBanner } from "./banner.js";
import { attachRerun } from "./rerun.js";

mountProvenanceBanner();

const recordSelect = document.querySelector("#record");
const questionSelect = document.querySelector("#question");
const output = document.querySelector("#output");
const status = document.querySelector("#status");

// The one piece of state this screen keeps, and it is a cache rather than a model: re-reading the
// same record from disk on every question change would be free and pointless.
let loaded = null;

load().catch(fail);

recordSelect.addEventListener("change", () => {
  loaded = null;
  load().catch(fail);
});
questionSelect.addEventListener("change", () => {
  show().catch(fail);
});

async function load() {
  if (!recordSelect.options.length) {
    const listing = await getJson("/showcase");
    const ids = listing.showcase_ids || [];
    if (!ids.length) {
      // Not an error, and not a blank page either. A build with no committed showcase is a
      // legitimate state — `showcase build` is a deliberate act that costs money — so the page says
      // which state it is in and what would change it.
      clear(output);
      output.append(
        el("section", { class: "alert" }, [
          el("h2", { text: "Nenhum showcase committado" }),
          el("p", {
            text:
              "Este build não tem nenhum registro em eval/showcase/. Isso é um estado legítimo: " +
              "gerar um showcase chama um provedor pago e é um ato deliberado, nunca automático. " +
              "Nada está sendo escondido — não há o que mostrar.",
          }),
          el("p", { class: "aside mono", text: "python -m garage showcase build --scope … --yes" }),
        ])
      );
      say("");
      return;
    }
    fillSelect(recordSelect, ids.map((id) => ({ value: id, text: id })), ids[0]);
  }

  say(`lendo ${recordSelect.value}…`);
  loaded = await getJson(`/showcase/${encodeURIComponent(recordSelect.value)}`);
  fillSelect(
    questionSelect,
    loaded.items.map((item) => ({ value: item.question_id, text: item.question })),
    loaded.items[0].question_id
  );
  await show();
}

async function show() {
  const item = loaded.items.find((candidate) => candidate.question_id === questionSelect.value);
  if (!item) throw new Error(`pergunta ${questionSelect.value} não está neste registro`);

  // Every identifier both columns name, asked for once. Deduplicated because the two strategies
  // routinely overlap and asking twice would be asking the database to answer a question this page
  // already knows the answer to.
  const ids = [
    ...new Set(item.arms.flatMap((arm) => arm.retrieval.chunks.map((chunk) => chunk.chunk_id))),
  ];
  const hydration = await getJson(`/chunks?${ids.map((id) => `ids=${encodeURIComponent(id)}`).join("&")}`);

  if (hydration.corpus_hash !== loaded.provenance.corpus_hash) {
    // Belt and braces. The service already refuses to boot against a record built on another
    // artifact (`showcase.verify_showcase_records`), so reaching this line means something changed
    // underneath a running process. Drawing anyway would put the wrong paragraph under a real
    // citation, which is the one failure a Glass Box must never produce quietly.
    throw new Error(
      `o registro foi construído sobre corpus_hash ${loaded.provenance.corpus_hash} e este ` +
        `serviço serve ${hydration.corpus_hash} — a hidratação foi recusada`
    );
  }

  const view = toView({
    source: "showcase",
    record: loaded,
    item,
    chunks: hydration.chunks,
    missing: hydration.missing,
  });

  clear(output);
  output.append(banner(view.showcase));
  output.append(whyThisQuestion(view.showcase));

  // The comparison, drawn by the component the live page uses, into a container the live page does
  // not have: `.precomputed` is what carries the hatch. Wrapping rather than restyling `.arm`
  // directly, so the texture is a property of *this screen* and cannot leak into the live one.
  const region = el("div", {
    class: "precomputed",
    attrs: {
      role: "region",
      "aria-label":
        `Resultados pré-computados do registro ${view.showcase.showcaseId}. Nenhuma chamada a ` +
        `modelo foi feita para exibir esta página.`,
    },
  });
  renderComparison(view, region);
  stampPanels(region, view);
  attachSpreads(region, view);
  // Last, so the re-run panel sits below the spread it is drawn against and is not stamped
  // "PRÉ-COMPUTADO" by `stampPanels` — it is the one panel on this screen that is not.
  attachRerun(region, view, item);
  output.append(region);

  say(
    `${view.showcase.showcaseId} · ${view.showcase.sampling.n} amostras por braço · ` +
      `nenhuma chamada ao modelo · ${view.showcase.hydrated} trechos hidratados, ` +
      `${view.showcase.absent} ausentes`
  );
}

// --- saying it is precomputed --------------------------------------------------------------------

function banner(showcase) {
  return el("section", { class: "shared precomputed-banner" }, [
    el("h2", { text: "Estes números são pré-computados" }),
    el("p", {
      text:
        "Nada nesta página foi medido agora. Todas as respostas, citações, traces e custos abaixo " +
        "foram amostrados de um provedor pago na data indicada e estão gravados em um arquivo " +
        "deste repositório. Abrir esta tela não chama modelo nenhum e não custa nada.",
    }),
    el("dl", { class: "kv" }, [
      el("dt", { text: "showcase_id" }),
      el("dd", {}, [
        el("span", { class: "mono", text: showcase.showcaseId }),
        // Said on screen, not only in the file. The id ends in twelve characters of a commit sha
        // and reads exactly like a `run_id`, which does keep the guarantee; a reader who is not
        // told otherwise will assume this one does too.
        showcase.gitDirty
          ? el("strong", {
              class: "warn-inline",
              text:
                " — árvore suja: o sha neste id é o do último commit, e não identifica o código " +
                "que produziu estes números. Reconstrua a partir de uma árvore limpa antes de citar.",
            })
          : null,
      ]),
      el("dt", { text: "escopo" }),
      // The field that keeps a three-question proving run from being read as the curated set. It is
      // required in the record precisely because the two are indistinguishable without it.
      el("dd", { text: showcase.scope }),
      el("dt", { text: "amostrado em" }),
      el("dd", { text: showcase.sampling.measuredOn }),
      el("dt", { text: "provedor · modelo" }),
      el("dd", { text: `${showcase.sampling.generator} · ${showcase.sampling.model ?? EM_DASH}` }),
      el("dt", { text: "n · temperatura" }),
      el("dd", {
        text: `${showcase.sampling.n} amostras por pergunta por braço · temperatura ${number(showcase.sampling.temperature, 1)}`,
      }),
      el("dt", { text: "amostra exibida" }),
      el("dd", { text: showcase.displayRule }),
      el("dt", { text: "texto de trecho no arquivo" }),
      el("dd", {
        // Printed as a fact about the file, not as reassurance. It is the ADR-0003 claim, and the
        // record carries it as a field so this line reads it rather than asserting it.
        text: showcase.redistribution.chunkTextStored
          ? "sim — isto não deveria ser possível"
          : `não. O arquivo guarda apenas chunk_id; o texto e a seção vêm de GET /chunks, local e ` +
            `gratuito. Portão de verbatim, duas medidas: maior trecho contíguo repetido ` +
            `${showcase.redistribution.worstRun} tokens (limite ${showcase.redistribution.runLimit}), ` +
            `maior subsequência em ordem ${showcase.redistribution.worstSubsequence} tokens ` +
            `(limite ${showcase.redistribution.subsequenceLimit})`,
      }),
    ]),
  ]);
}

function whyThisQuestion(showcase) {
  // Required in the record and therefore always present. A showcase is a set of questions somebody
  // chose, and the reason for each choice is the difference between a benchmark and a highlight
  // reel — so the reason is on screen beside the result, not in a commit message.
  return el("section", { class: "note" }, [
    el("strong", { text: "Por que esta pergunta está no showcase" }),
    el("p", { text: showcase.why }),
  ]);
}

function stampPanels(region, view) {
  // Channel 2, and — since the QA round — the place the sample qualifier lives too.
  //
  // The cost panel prints `tokens`, `custo` and the waterfall prints `generate_ms`: every one of
  // those is **one draw of n**, and they were rendering as lone numbers with the qualifier several
  // hundred pixels below in the spread panel. That does not break ADR-0004's letter — no scalar for
  // a stochastic metric exists in the file, and the spread is right there — but a reader who never
  // scrolls has read a point value off the screen, which is what ADR-0004 is *for*. So "amostra k
  // de n" travels in the stamp, and the stamp is on every panel.
  //
  // Done by walking the rendered DOM rather than by threading a flag through `render.js`: the
  // alternative is every component learning about a screen it has nothing to do with. `.panel`,
  // `.state` and `[data-strategy]` are class names those components already publish, so this reads
  // a contract rather than guessing at markup.
  const showcase = view.showcase;
  for (const arm of view.arms) {
    const column = region.querySelector(`.arm[data-strategy="${cssEscape(arm.strategy)}"]`);
    if (!column) continue;
    // A failed arm has no samples and gets the plain stamp: there is no draw to qualify.
    const draw =
      arm.failed || arm.displayedSample === undefined
        ? null
        : `amostra ${arm.displayedSample + 1} de ${arm.sampleCount}`;
    for (const panel of column.querySelectorAll(".panel, .state")) {
      panel.prepend(stamp(showcase, draw));
    }
  }
}

function stamp(showcase, draw) {
  const parts = ["PRÉ-COMPUTADO", ...(draw ? [draw] : []), showcase.showcaseId];
  return el("p", {
    class: "stamp",
    attrs: {
      "aria-label": draw
        ? `valor pré-computado, ${draw}, registro ${showcase.showcaseId}`
        : `valor pré-computado, registro ${showcase.showcaseId}`,
    },
    text: parts.join(" · "),
  });
}

// --- the spread ------------------------------------------------------------------------------------

function attachSpreads(region, view) {
  for (const arm of view.arms) {
    if (arm.failed || !arm.spread) continue;
    // `data-strategy` is set by `render.armColumn` and is the handle this screen hangs off. One
    // column per strategy is guaranteed by the record format itself, which forbids two arms of the
    // same strategy in one item.
    const column = region.querySelector(`.arm[data-strategy="${cssEscape(arm.strategy)}"]`);
    if (!column) continue;
    column.append(spreadPanel(arm, view.showcase));
  }
}

function spreadPanel(arm, showcase) {
  return el("article", { class: "panel" }, [
    // The one panel whose numbers are *not* a single draw, so its stamp says so instead of naming
    // one. Appended after `stampPanels` has run, which is why it carries its own.
    el("p", {
      class: "stamp",
      attrs: {
        "aria-label": `dispersão pré-computada sobre as ${arm.sampleCount} amostras, registro ${showcase.showcaseId}`,
      },
      text: `PRÉ-COMPUTADO · todas as ${arm.sampleCount} amostras · ${showcase.showcaseId}`,
    }),
    el("h4", { text: `Dispersão entre as ${arm.sampleCount} amostras` }),
    el("p", {
      class: "aside",
      text:
        `Cada marca é uma amostra, na ordem em que foi sorteada. Sem barra de erro, sem média e ` +
        `sem desvio-padrão: ${arm.sampleCount} amostras não sustentam um sigma, e o registro não ` +
        `guarda nenhum valor pontual para grandeza estocástica nenhuma. Exibida na coluna ao lado: ` +
        `amostra ${arm.displayedSample + 1} de ${arm.sampleCount}, por ${arm.displayRule}.`,
    }),
    el("div", { class: "spreads" }, arm.spread.map((metric) => spreadRow(metric, arm))),
  ]);
}

function spreadRow(metric, arm) {
  const format = metric.key === "cost_usd" ? (value) => usd(value) : (value) => number(value, digitsFor(metric.key));
  return el("div", { class: "spread-row" }, [
    el("div", { class: "spread-head" }, [
      el("span", { class: "spread-label", text: metric.label }),
      el("span", {
        class: metric.constant ? "spread-range constant" : "spread-range",
        // Three sentences, not two, and the third one is the fix for a real defect this screen
        // showed in a browser: an all-null metric — `cost_usd` and `generate_ms` under a zero-cost
        // abstention, where nobody was called — has `distinct: 0` and rendered as "— idêntico nas 3
        // amostras", which reads as three identical measurements of nothing. There were no
        // measurements at all, and that is a different fact.
        text: metric.distinct === 0
          ? `sem valor nas ${metric.n} amostras — não houve chamada`
          : metric.constant
            ? `${format(metric.minimum)} · idêntico nas ${metric.n} amostras`
            : `${format(metric.minimum)} — ${format(metric.maximum)} · ${metric.distinct} valores distintos`,
      }),
    ]),
    stripPlot(metric, arm),
  ]);
}

function stripPlot(metric, arm) {
  const track = el("div", { class: "strip" });
  for (const mark of metric.marks) {
    if (mark.fraction === null) continue; // a null was not a value and gets no position
    track.append(
      el("span", {
        class: mark.index === arm.displayedSample ? "strip-mark strip-shown" : "strip-mark",
        style: { left: mark.fraction * 100 },
        attrs: {
          // Every mark is reachable and announced. The strip is the only place the individual draws
          // exist on screen, and a plot a screen reader cannot enumerate would put them back out of
          // reach of exactly the reader who most needs the numbers rather than the picture.
          tabindex: "0",
          role: "listitem",
          "aria-label":
            `amostra ${mark.index + 1}: ${mark.value}` +
            (mark.index === arm.displayedSample ? " (a exibida)" : ""),
          title: `amostra ${mark.index + 1}: ${mark.value}`,
        },
      })
    );
  }
  track.setAttribute("role", "list");
  track.setAttribute("aria-label", `${metric.n} amostras de ${metric.label}`);
  return track;
}

function digitsFor(key) {
  // Token counts and claim counts are integers and must not read as `812,000`; latency gets one
  // decimal, like every other millisecond on this site.
  if (key === "generate_ms") return 1;
  return 0;
}

// --- plumbing ----------------------------------------------------------------------------------------

async function getJson(path) {
  const response = await fetch(path);
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    throw new Error(`${path}: HTTP ${response.status} — ${detail.detail ?? ""}`);
  }
  return response.json();
}

function fillSelect(select, options, selected) {
  clear(select);
  select.disabled = false;
  for (const option of options) {
    select.append(el("option", { text: option.text, attrs: { value: option.value } }));
  }
  select.value = selected;
}

function say(text) {
  status.className = "status";
  status.textContent = text;
}

function fail(failure) {
  status.className = "status status-error";
  status.textContent = failure.message;
}

function cssEscape(value) {
  return window.CSS && window.CSS.escape ? window.CSS.escape(value) : value.replace(/["\\]/g, "\\$&");
}
