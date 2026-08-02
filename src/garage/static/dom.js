// The only place this interface is allowed to build a DOM node.
//
// Why a helper at all, when the brief says no mini-framework: because `innerHTML` is banned here and
// a ban nobody can follow is not a ban. `chunk.text` comes out of the corpus, `answer.text` and
// `claim.text` come out of a language model, and `exception.message` comes out of whatever the
// provider felt like sending. Every one of those is attacker-adjacent text rendered on a page, and
// the single line below — `node.textContent = ...` — is what makes an injected `<script>` show up as
// characters on screen instead of running. Twenty lines of helper buys the property that no calling
// file ever has to remember it.
//
// It is not a component system and must not grow into one: no state, no lifecycle, no diffing. It
// takes values and returns an `Element`, which is exactly the surface React replaces for free.

export function el(tag, options = {}, children = []) {
  const node = document.createElement(tag);
  if (options.class) node.className = options.class;
  // `textContent`, never `innerHTML`. See above.
  if (options.text !== undefined && options.text !== null) node.textContent = String(options.text);
  // Geometry, set through the CSSOM one property at a time — never `setAttribute("style", …)` and
  // never `cssText`.
  //
  // This is a Content-Security-Policy requirement and not a style preference. The public deployment
  // serves `style-src 'self'` (`deploy/Caddyfile`), which blocks the `style` *attribute* outright:
  // every bar in the waterfall, every tick on a score axis and every mark on a strip plot silently
  // failed to position, so the page rendered as a column of empty tracks with no error a visitor
  // could see. `element.style.setProperty` is the CSSOM path and is not covered by `style-src`,
  // which is exactly the distinction the policy is drawing — a value assigned by this code is not a
  // stylesheet the page received from anywhere.
  //
  // Numbers only, enforced below, and that is the second reason this is a separate channel. Every
  // caller is placing something by percentage from an arithmetic result; nothing here ever
  // interpolates a served string into CSS, and a `Number.isFinite` gate makes that a property of the
  // helper rather than a habit of the callers. A `NaN` — which a null offset used to produce — is
  // dropped instead of written, so a missing measurement leaves the element unpositioned rather than
  // parked at the left edge pretending to be zero.
  for (const [property, value] of Object.entries(options.style || {})) {
    if (typeof value !== "number" || !Number.isFinite(value)) continue;
    node.style.setProperty(property, `${value}%`);
  }
  for (const [name, value] of Object.entries(options.attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    // The one attribute this helper refuses to write. See above: it is blocked by the deployment's
    // CSP, so a caller reaching for it would produce an element that positions correctly in
    // development and not at all in production. `style` is a percentage map, and only that.
    if (name === "style") {
      throw new Error('el(): use `style: {left: 42}` (percent) rather than an inline style attribute');
    }
    node.setAttribute(name, value === true ? "" : String(value));
  }
  for (const [name, value] of Object.entries(options.data || {})) {
    if (value === null || value === undefined) continue;
    node.dataset[name] = String(value);
  }
  for (const [name, handler] of Object.entries(options.on || {})) {
    node.addEventListener(name, handler);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(typeof child === "string" || typeof child === "number" ? String(child) : child);
  }
  return node;
}

export function clear(node) {
  node.replaceChildren();
  return node;
}

// Formatting lives here rather than in the adapter, and the split is deliberate: the adapter's job
// is to say *what is missing*, the formatter's job is to say how missing looks. Keeping them apart
// is what stops a `null` from being helpfully turned into a `0` somewhere upstream of the screen.

const NUMBER_LOCALE = "pt-BR";

export function number(value, digits = 3) {
  if (value === null || value === undefined) return EM_DASH;
  return value.toLocaleString(NUMBER_LOCALE, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

export const EM_DASH = "—";

export function milliseconds(value) {
  // Never `0 ms` for a stage that did not run: the caller passes null and reads a dash back. A zero
  // millisecond bar is a trace claiming a stage executed instantly, which is a different and much
  // more flattering statement than "it never happened".
  if (value === null || value === undefined) return EM_DASH;
  return `${value.toLocaleString(NUMBER_LOCALE, { maximumFractionDigits: 1 })} ms`;
}

export function usd(value) {
  // `null` is not zero. A model with no published price and a call that never happened both arrive
  // here as null, and both must read as "no figure" — a free-looking row in a cost comparison is the
  // one number in this interface that would actively mislead.
  if (value === null || value === undefined) return EM_DASH;
  // The same locale as every other number on the page. It was `en-US`, which put `US$ 0.000414`
  // directly above `0,700000 RRF` on a document declaring `lang="pt-BR"` — where a decimal point is
  // a thousands separator and that figure reads as four hundred and fourteen. The currency is USD
  // and stays USD; how a Brazilian reader parses the digits is a separate question from which
  // currency they denominate.
  return `US$ ${value.toLocaleString(NUMBER_LOCALE, { minimumFractionDigits: 6, maximumFractionDigits: 6 })}`;
}

const SUPERSCRIPT_DIGITS = "⁰¹²³⁴⁵⁶⁷⁸⁹";
const SUPERSCRIPT_TIERS = { A: "ᴬ", B: "ᴮ" };

export function superscript(index, tier) {
  const digits = String(index)
    .split("")
    .map((character) => SUPERSCRIPT_DIGITS[Number(character)] ?? character)
    .join("");
  // The tier letter travels inside the glyph itself. A superscript number alone would make the
  // manufacturer's torque figure and someone's forum recollection look identical at the one place a
  // reader actually looks — mid-sentence (design §13).
  return digits + (SUPERSCRIPT_TIERS[tier] ?? "");
}
