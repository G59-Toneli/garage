// The fixture banner: a permanent, non-dismissible statement that the documents are invented.
//
// Every screen imports this and nothing else has to remember it. That is the point — the rule is
// "while the corpus is the fixture, say so", and a rule enforced by three separate `<div>`s in three
// HTML files is a rule that will be true on two screens after the next change.
//
// ## Why it is not optional and not dismissible
//
// This project's claim is that a published number is something a visitor can try to falsify
// (ADR-0004). The fixture Corpus in `corpus/fixture/` is a set of documents that were **written for
// this repository**: a service manual that does not exist, forum threads nobody posted. Falsifying a
// claim about invented material is an exercise, and the site has to say that out loud or it is
// running the exercise while looking like the real thing.
//
// A dismiss button would make it true for the first thirty seconds of a session and false afterwards
// — and the reader most likely to dismiss it is the reader most likely to quote a number from the
// page. So there is no dismiss button, and this file has no state.
//
// ## Why it is driven by a field
//
// It keys off `provenance.corpus_id === "fixture"` rather than off a constant, so it disappears by
// itself on the day issue #10 catalogues real material — and, more importantly, it is impossible to
// forget to remove and impossible to remove early. The condition is the fact.
//
// `GET /provenance` is one request, cached by nothing, costing nothing, and it is also where the
// commit sha and the day's generation budget come from. A failure to read it is *not* silent: a page
// that could not find out whether its corpus is invented must not quietly render as though it had
// found out that it is not.

import { el } from "./dom.js";

const FIXTURE_CORPUS_ID = "fixture";

export async function mountProvenanceBanner(root = document.body) {
  let payload;
  try {
    const response = await fetch("/provenance");
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    payload = await response.json();
  } catch (failure) {
    // The conservative direction. If we cannot tell whether the material is invented, we say we
    // cannot tell — because the alternative is a page that looks exactly like a page standing on a
    // real corpus.
    root.prepend(unknownBanner(failure.message));
    return null;
  }

  if (payload.corpus_id === FIXTURE_CORPUS_ID) root.prepend(fixtureBanner(payload));
  return payload;
}

function fixtureBanner(payload) {
  return el(
    "section",
    {
      class: "fixture-banner",
      attrs: {
        role: "note",
        "aria-label":
          "Aviso permanente: o corpus deste site é fictício e os documentos foram inventados para o projeto.",
      },
    },
    [
      el("strong", { text: "Este corpus é fictício." }),
      el("span", {
        text:
          " Os documentos abaixo — manual de serviço, boletins, tópicos de fórum — foram inventados " +
          "para este projeto e não descrevem nenhum veículo real. Tudo o que esta página mostra " +
          "sobre recuperação, citação e custo é medido de verdade; o material sobre o qual é medido, " +
          "não. Falsificar uma afirmação sobre material inventado é um exercício, e o site diz isso " +
          "em vez de deixar você descobrir.",
      }),
      el("span", { class: "aside mono", text: ` corpus_id=${payload.corpus_id} · build ${payload.git_sha}` }),
    ]
  );
}

function unknownBanner(reason) {
  return el("section", { class: "fixture-banner fixture-banner-unknown", attrs: { role: "note" } }, [
    el("strong", { text: "Não foi possível verificar a procedência do corpus." }),
    el("span", {
      text:
        " Esta página não conseguiu ler /provenance, então não sabe se o material é o corpus " +
        "fictício do repositório ou material real. Trate os números abaixo como não verificados.",
    }),
    el("span", { class: "aside mono", text: ` ${reason}` }),
  ]);
}
