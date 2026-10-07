# Optional semantic review protocol

Deterministic code should decide everything software can establish reliably:
status codes, robots rules, canonical tags, headings, link counts, JSON-LD
parsing, dates, duplicates, byte counts. It does, and those checks carry the
report.

A small number of judgements genuinely benefit from reading the page: whether an
opening paragraph actually explains the offering, whether a brand name is
ambiguous *in its category*, whether a description is specific enough to be
worth quoting. This document governs that pass.

**It is optional.** Skipping it produces a complete, valid report — the
deterministic checks already cover these areas conservatively. Its purpose is to
*add* precision, never to supply the report's substance.

---

## Inputs

Run the audit with `--agent-context-out ctx.json`. The packet is deliberately
small (a few kilobytes) so this pass never requires reading the crawl:

| Field | What it gives you |
|---|---|
| `entry_page` | URL, title, H1, meta description, word count, opening ~700 characters. |
| `sampled_pages[]` | The same digest for up to six pages. |
| `navigation_labels[]` | The site's own menu wording. |
| `action_labels[]` | Button-styled link labels. |
| `declared_identity_jsonld[]` | Identity markup as published. |
| `checks_requesting_semantic_review[]` | Checks tagged `semantic-review`, with their evidence and status. |
| `deterministic_findings[]` | What has already been found, so you do not repeat it. |

---

## Rules

1. **Evidence only from the packet.** Every claim must be supported by text that
   is in `ctx.json`. Do not fetch pages, do not recall the brand from training
   data, do not reason about what the site "probably" has elsewhere.
2. **No duplicates.** If a deterministic finding already covers it, say nothing.
   Adjusting an existing finding is done by referencing its `check_id`, not by
   adding a second one.
3. **Severity is capped.** Semantic findings use `impact_base ≤ 3` and
   `confidence: "medium"` at best, so the model applies the medium cap and they
   can never be graded `critical`. A judgement call is not an emergency.
4. **Quote.** `evidence` must contain the actual wording being judged, in quotes,
   with the URL. "The value proposition is unclear" is not evidence; *"The H1
   reads 'Empowering next-generation outcomes', which names no product, no
   audience and no category"* is.
5. **No taste.** Colour, tone, layout, brand voice and word preference are out of
   scope. Judge only whether a **machine or a first-time visitor could extract
   the meaning**.
6. **Prefer silence.** Returning zero findings is a good outcome and the correct
   one whenever the deterministic checks already passed. Padding this pass is
   the fastest way to make the report worse.

---

## Output

Write `mine.json`:

```json
{
  "producer": "agent-semantic-review",
  "findings": [
    {
      "check_id": "AG-01",
      "title": "The home page never states what the product does",
      "pillar": "ai_discoverability",
      "category": "fact_extraction",
      "impact_base": 3,
      "confidence": "medium",
      "breadth": { "affected": 1, "applicable": 1, "unit": "pages" },
      "evidence": "The entry page H1 is \"Empowering next-generation outcomes\" and the first 700 characters contain no noun naming a product, service or industry. A reader cannot tell from this page what is sold or to whom.",
      "evidence_items": [
        { "url": "https://example.com/",
          "observed": "H1: 'Empowering next-generation outcomes'; opening text names no category",
          "expected": "a heading naming the offering and the audience" }
      ],
      "mechanism": "Retrieval matches a question against the page's own words. Copy that names no category matches no query, so the page is never retrieved for the thing the company actually sells.",
      "remediation": {
        "summary": "Replace the hero heading with one naming the product category and the audience.",
        "steps": ["Write the H1 as '<what it is> for <who it is for>' using the words a buyer would type.",
                  "Follow it with two sentences containing one verifiable specific."],
        "expected_benefit": "The page becomes retrievable for its actual category.",
        "effort": "low"
      },
      "dedupe_key": "value-proposition",
      "applicability": "Judged from the entry page digest in the agent context packet."
    }
  ]
}
```

Then merge:

```bash
python ../scripts/finalize_report.py --report report.json \
    --agent-findings mine.json --out report.json
```

`finalize_report.py` re-applies the whole composition pipeline — de-duplication
by `dedupe_key`, the severity model, deterministic ordering, ID reassignment,
summary recount and schema validation. Reusing an existing `dedupe_key` (as
above) merges your finding with the deterministic one rather than adding a
second entry.

---

## Where this pass helps most

| Deterministic check | What it can prove | What it hands over |
|---|---|---|
| `AC-11` value proposition | No H1, a placeholder H1, a brand-only H1, under 15 words of opening text. | Whether prose that *is* present actually communicates the offering. |
| `AC-12` fact density | Counts figures, dates, prices and definitional verbs. | Whether the statements present are specific enough to quote. |
| `FC-09` entity disambiguation | Which of five disambiguating attributes are absent. | Whether the name is genuinely ambiguous in its category. |
| `EN-01` primary action | Whether any action-labelled control exists. | Whether the label communicates the outcome. |

In each case the deterministic check has already reported what it can prove.
This pass either confirms it in the site's own words, or — just as usefully —
finds that the automated verdict was too harsh and says so.
