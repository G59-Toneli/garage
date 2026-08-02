// Wiring: read the form, call the API twice, adapt, render. Nothing else lives here.

import { el, clear } from "./dom.js";
import { toView } from "./adapt.js";
import { renderComparison } from "./render.js";

// No strategy name is written down in this file or in the HTML any more. The selects start empty
// and disabled and are filled by `GET /strategies`, so a lexical-only build never shows a visitor a
// "dense" option — not even for the one round trip a hard-coded default used to leave it on screen.

const form = document.querySelector("#ask");
const questionInput = document.querySelector("#question");
const kInput = document.querySelector("#k");
const tierInputs = [...document.querySelectorAll("input[name=tier]")];
const contractInput = document.querySelector("#contract");
const leftSelect = document.querySelector("#left");
const rightSelect = document.querySelector("#right");
const submitButton = document.querySelector("#ask button[type=submit]");
const output = document.querySelector("#output");
const status = document.querySelector("#status");

form.addEventListener("submit", (event) => {
  event.preventDefault();
  ask().catch((failure) => fail(failure.message));
});

// Nothing recovers this page if the strategy list cannot be read, so nothing pretends to. The
// controls are born disabled and are enabled only by a successful `GET /strategies`, which means a
// silent failure here leaves an interface that is permanently unusable and says nothing — the exact
// shape of bug this project refuses everywhere else. An absence travels as an absence: the page says
// what it could not find out, and offers to try again.
refreshStrategies().catch((failure) => unavailable(failure.message));

async function ask() {
  const question = questionInput.value.trim();
  if (!question) return;
  const tiers = tierInputs.filter((input) => input.checked).map((input) => input.value);
  const body = {
    question,
    k: Number(kInput.value),
    tiers,
    contract: contractInput.value,
  };

  say("consultando as duas estratégias…");
  // Two ordinary `POST /query` calls, in parallel, one per strategy — and no `/compare` endpoint.
  // "Two columns" is a decision this file makes; an endpoint shaped around it would be that decision
  // leaking into the API, and the acceptance criteria never asked for comparable timings.
  //
  // The consequence, stated rather than hidden: two independent `Tracer`s, so two unrelated
  // `traceId`s with no parent/child relation between them. That is normal and the panel shows both.
  //
  // `allSettled` and not `all`, and the difference is the whole point of the screen. `all` rejects
  // on the first failure, so a 429 on one arm would erase the other arm's chunks, answer and trace
  // — a comparison destroyed by a rate limit on one side of it. Each column reports its own failure
  // and the other one still renders.
  const strategies = [leftSelect.value, rightSelect.value];
  const settled = await Promise.allSettled(strategies.map((strategy) => query({ ...body, strategy })));

  const responses = settled.map((result, index) =>
    result.status === "fulfilled"
      ? result.value
      : // A failed arm becomes a *view source* like any other rather than an exception thrown past
        // the renderer, so the adapter stays the only thing that knows what a column is made of.
        { failed: true, strategy: strategies[index], error: result.reason.message }
  );

  clear(output);
  const view = toView({ source: "live", responses });
  renderComparison(view, output);

  const failures = responses.filter((response) => response.failed);
  if (!view.shared.agrees) {
    // The comparison was refused, so no success line: "duas requisições paralelas" under a panel
    // saying the two arms are not comparable reads as though it worked.
    fail("corpus_hash divergente entre as colunas — a comparação foi recusada");
  } else if (failures.length === strategies.length) {
    fail(failures.map((response) => response.error).join(" · "));
  } else if (failures.length) {
    fail(`${failures.map((response) => response.error).join(" · ")} — a outra coluna continua válida`);
  } else {
    say(
      `${strategies.join(" vs ")} · duas requisições paralelas · tempo de parede, uma amostra, conexão fria`
    );
  }
}

async function query(body) {
  const response = await fetch("/query", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const detail = await response.json().catch(() => null);
    // Named, not interpolated blindly: with an empty `strategy` the old template produced a message
    // beginning with a bare colon.
    const named = body.strategy || "estratégia não informada";
    throw new Error(`${named}: HTTP ${response.status}${detail ? ` — ${describe(detail)}` : ""}`);
  }
  return response.json();
}

function describe(detail) {
  if (!detail || !Array.isArray(detail.detail)) return JSON.stringify(detail);
  return detail.detail.map((item) => item.msg).join("; ");
}

function unavailable(reason) {
  fail(`não foi possível descobrir quais estratégias este build serve — ${reason}`);
  clear(output);
  output.append(
    el("section", { class: "alert" }, [
      el("h2", { text: "Interface indisponível" }),
      el("p", {
        text:
          "Esta página precisa saber quais estratégias o serviço serve antes de poder compará-las, " +
          "e não conseguiu ler essa lista. Nada foi consultado e nada abaixo está sendo escondido: " +
          "não há o que mostrar.",
      }),
      el("p", { class: "aside mono", text: reason }),
      el("button", {
        text: "Tentar novamente",
        attrs: { type: "button" },
        // Deliberately not an automatic retry loop. A service that is down stays down, and a page
        // quietly polling it looks identical to a page that is working — which is the failure this
        // whole block exists to avoid.
        on: {
          click() {
            say("relendo /strategies…");
            refreshStrategies()
              .then(() => {
                clear(output);
                say("pronto");
              })
              .catch((failure) => unavailable(failure.message));
          },
        },
      }),
    ])
  );
}

async function refreshStrategies() {
  // `GET /strategies`, and nothing clever.
  //
  // The previous version of this function sent a deliberately invalid strategy and read the served
  // list out of the 422's `msg` with a regular expression. That worked, and was wrong twice over: it
  // parsed a human sentence, and it put a red error in the console of every visitor loading a page
  // that was working perfectly.
  //
  // No re-ordering happens here any more either. The endpoint publishes the list in the order
  // `available_retrievers` returns it — the pipeline's order — so which arm opens on the left is
  // decided by the module that owns retrieval, not by an alphabetisation in an error handler.
  // Every exit below throws. They used to return quietly, which was harmless while the HTML carried
  // placeholder options and stopped being harmless the moment those were removed: the three silent
  // exits became three ways to reach a dead page with a blank status line.
  const response = await fetch("/strategies");
  if (!response.ok) throw new Error(`GET /strategies respondeu HTTP ${response.status}`);
  const payload = await response.json();
  const served = (payload.strategies || []).map((strategy) => strategy.name);
  if (!served.length) throw new Error("este build não publicou nenhuma estratégia");
  fillSelect(leftSelect, served, served[0]);
  fillSelect(rightSelect, served, served[served.length - 1]);
  submitButton.disabled = false;
}

function fillSelect(select, names, selected) {
  clear(select);
  select.disabled = false;
  for (const name of names) {
    select.append(el("option", { text: name, attrs: { value: name, selected: name === selected } }));
  }
  select.value = selected;
}

function say(text) {
  status.className = "status";
  status.textContent = text;
}

function fail(text) {
  status.className = "status status-error";
  status.textContent = text;
}
