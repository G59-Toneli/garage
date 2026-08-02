// The metrics screen: the promoted baseline and the newest committed run record, side by side.
//
// Every number here is read out of `eval/baseline.json` and `eval/runs/*.json` over HTTP. None of it
// is typed into this repository's JavaScript — that is acceptance criterion six, and it is also why
// `0.911765` and `0.071429` appear nowhere in this file.
//
// It is the second consumer of the adaptation boundary, and the point of it: `toView` takes a
// `{source:"record"}` and the rendering below never sees a raw payload. Issue #11 extends the same
// branch rather than adding a third path.

import { el, clear, number, EM_DASH } from "./dom.js";
import { toView } from "./adapt.js";

const output = document.querySelector("#output");
const status = document.querySelector("#status");

load().catch((failure) => {
  status.className = "status status-error";
  status.textContent = failure.message;
});

async function load() {
  const baseline = await getJson("/eval/baseline");
  // The promoted baseline points at a `run_id`; the newest record in `eval/runs/` may be a later
  // measurement of the same build. Both are shown, because "what we promised" and "what we just
  // measured" are different claims and the gate compares exactly those two.
  const listing = await getJson("/eval/runs");
  const newest = listing.run_ids.length ? await getJson(`/eval/runs/${listing.run_ids[0]}`) : null;

  const view = toView({ source: "record", baseline, record: newest });
  clear(output);
  output.append(header(view));
  for (const arm of view.arms) output.append(armTable(arm, view));
  status.textContent = `${view.arms.length} braços · ${view.sampleCount} perguntas`;
}

async function getJson(path) {
  const response = await fetch(path);
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    throw new Error(`${path}: HTTP ${response.status} — ${detail.detail ?? ""}`);
  }
  return response.json();
}

function header(view) {
  return el("section", { class: "shared" }, [
    el("h2", { text: "Avaliação determinística" }),
    el("dl", { class: "kv" }, [
      el("dt", { text: "baseline promovido" }),
      el("dd", { class: "mono", text: view.promotedRunId }),
      el("dt", { text: "registro mais recente" }),
      el("dd", { class: "mono", text: view.measuredRunId ?? EM_DASH }),
      el("dt", { text: "perguntas" }),
      el("dd", { text: view.sampleCount }),
      el("dt", { text: "facts_sha256" }),
      el("dd", { class: "mono small", text: view.factsSha256 }),
      el("dt", { text: "tolerância" }),
      el("dd", { text: `${number(view.tolerance, 3)} — número de política, não estatístico` }),
      el("dt", { text: "piso de ruído" }),
      el("dd", { text: number(view.noiseFloor, 3) }),
    ]),
    el("p", {
      class: "aside",
      // Said once, plainly, because the absence of error bars on this screen is a decision and not
      // an omission: these metrics are computed from a fixed question set against a fixed artifact
      // and have no variance to draw. A whisker on a number with no variance is an invented number.
      text:
        "Sem barras de erro: estas métricas são determinísticas — mesma pergunta, mesmo artefato, " +
        "mesmo resultado. O que está desenhado é o corredor de tolerância, que é política de gate.",
    }),
    view.provenance.length
      ? el("details", {}, [
          el("summary", { text: "proveniência do registro" }),
          el(
            "dl",
            { class: "kv" },
            view.provenance.flatMap((entry) => [
              el("dt", { class: "mono small", text: entry.key }),
              el("dd", { class: "mono small", text: String(entry.value) }),
            ])
          ),
        ])
      : null,
  ]);
}

function armTable(arm, view) {
  const rows = arm.metrics.map((metric) => metricRow(metric));
  return el("section", { class: "panel" }, [
    el("h3", { text: arm.strategy }),
    el("p", { class: "aside", text: `embedder ${arm.embedder ?? "—"} · k ${arm.k} · tiers ${arm.tiers.join(", ")} · reranker ${arm.reranker ?? "—"}` }),
    el("table", { class: "metrics" }, [
      el("thead", {}, [
        el("tr", {}, [
          el("th", { text: "métrica" }),
          el("th", { text: "promovido" }),
          el("th", { text: "medido" }),
          el("th", { text: "delta" }),
          el("th", { text: `corredor (tolerância ${number(view.tolerance, 3)})` }),
        ]),
      ]),
      el("tbody", {}, rows),
    ]),
  ]);
}

function metricRow(metric) {
  const delta = metric.delta;
  return el("tr", { class: metric.gated ? "gated" : "ungated" }, [
    el("th", {}, [
      el("span", { text: metric.name }),
      metric.gated ? el("span", { class: "badge", text: "GATED" }) : el("span", { class: "aside", text: " não gated" }),
    ]),
    el("td", { class: "num", text: number(metric.promoted, 6) }),
    el("td", { class: "num", text: number(metric.measured, 6) }),
    el("td", {
      class: delta === null ? "num" : delta < 0 ? "num worse" : "num",
      text: delta === null ? EM_DASH : `${delta >= 0 ? "+" : ""}${number(delta, 6)}`,
    }),
    el("td", {}, [corridor(metric)]),
  ]);
}

function corridor(metric) {
  // A 0..1 axis, the promoted value as a tick, and the band between the promoted value and the floor
  // the gate allows before it fails the build. The band is policy, drawn as policy — it says nothing
  // about how much a measurement might wobble, because it does not wobble.
  const track = el("div", { class: "corridor" });
  if (metric.floor !== null && metric.promoted !== null) {
    track.append(
      el("div", {
        class: "corridor-band",
        attrs: { style: `left:${metric.floor * 100}%;width:${(metric.promoted - metric.floor) * 100}%` },
      })
    );
  }
  if (metric.promoted !== null) {
    track.append(el("div", { class: "corridor-mark", attrs: { style: `left:${metric.promoted * 100}%` } }));
  }
  if (metric.measured !== null) {
    track.append(el("div", { class: "corridor-measured", attrs: { style: `left:${metric.measured * 100}%` } }));
  }
  return track;
}
