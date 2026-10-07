# Language surfaces: what "served" means and where it comes from

## The one assertion that matters

**The response's `language` field equals the requested `Accept-Language` tag.**

Not "the request succeeded". A 200 that echoes `zh-Hant` while carrying
Simplified copy is the exact failure this workstream spent many tickets removing
(`docs/reviews/2026-10-07-i18n-program-retrospective.md`). Every surface that
resolves a language reports what it ACTUALLY served, so the field is checkable
without reading the copy — and reading the copy is not a reliable test anyway,
because a translator may legitimately leave proper nouns in Latin script.

Per-row vs per-list: `/v1/shortcuts` reports `language` on each row AND at the
list level. The list-level value is only truthful when every row agrees; when
rows disagree, the per-row value is the one a client must read. A row published
before a language existed serves `zh` and SAYS `zh` — honest, not a bug.

## Where the language comes from

```
Accept-Language  →  i18n.resolve_language  →  a declared tag  →  the copy table
                     (RFC 7231 q-values)      (zh-Hant kept     (falls back to zh)
                                               as itself)
```

Three places this has silently collapsed, all fixed, all worth re-checking if
you touch them:

1. **The resolver folded script subtags.** `zh-Hant` → `zh`. Fixed; `zh-Hant-TW`
   also keeps its script now.
2. **Persistence truncated the tag.** Even a correct resolution was stored as
   `zh`. Fixed in `gateway_store._language`.
3. **"Non-Chinese" was derived from "not the default".** That classified
   Traditional as non-Chinese, so the output guard withheld **every legitimate
   Traditional answer**. It is a declared per-language property now.

## What is a defect and what is not

| Observation | Verdict |
|---|---|
| `language` == requested, copy non-empty | ✅ |
| `language` == requested, copy is the authority language | ❌ the false-statement shape |
| `language` == `zh` while `zh-Hant` was requested | ❌ unless the row predates the tag — check the row's `language` |
| Contract identifiers (`code`/`status`/`unit`) untranslated | ✅ correct — clients branch on them |
| Resource names (`media[].title`) untranslated | ✅ correct — translating them orphans the asset |
| Thai/Khmer free text refused with a Thai/Khmer message | ✅ correct — 能读不能问, declared |

## Coverage is not correctness

Coverage (does every table have this language) is asserted by
`tests/test_i18n_acceptance_11_languages.py`. **Translation quality has never
been sampled** — the code checks structure, not whether the copy reads well. Do
not report a green suite as "the translations are good".
