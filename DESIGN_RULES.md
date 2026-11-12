# LedgerSight design rules (v7)

These rules are written for anyone changing the LedgerSight UI. Every page follows them. They are enforced by the last
layer of `ledgersight/static/avocet.css` (the "DESIGN RULES (v7)" block), so older layers cannot override them.
`tests/e2e/e2e.py` ("Design rules hold on every page") fails if something breaks them.

| # | Element | Rule |
|---|---|---|
| 1 | **Card** (top-level box on a page, including review-queue cases) | White (`--sk-card`), no border, soft shadow (`--sk-shadow`), 24px corners. |
| 2 | **Box inside a card** (source rows, facts, proof tiles, agent stats, notes) | Tinted fill (`--ds-inset`), no border, no shadow, 16px corners (20px for larger tiles). |
| 3 | **Status** | A badge (pill with a dot) or a round icon. Never a coloured stripe on a box's edge. |
| 4 | **Buttons** | Pill shape. 42px by default, 34px for compact spots. Icon-only buttons are round. Main action: blue gradient (`.primary`). Secondary: dark outline (`.secondary`). Approve: green (`.ok`). Reject/delete: red outline (`.no`, `.danger`). Compact outline: `.ghost` / `.pillbtn`. |
| 5 | **Fields** | 42px tall, matching buttons. 12px corners. Search and Ask fields are pills. |
| 6 | **Badges, chips, small status shapes** | Pill (999px) or circle. |
| 7 | **Tables** | No outer frame. Thin lines between rows only. |
| – | **Type** | Urbanist everywhere; monospace only for ids, hashes and file names. Numbers use tabular figures. |
| – | **Scrollbars** | Never visible. Boxes still scroll by wheel, trackpad, touch and keyboard. |
| – | **Colour (light mode)** | 60 / 30 / 10. Background: soft powder blue (`--sk-panel` #DFEBF6 main area, `--sk-page` #EEF4FA edge). Surfaces: white cards, sidebar and top-bar pills; boxes inside cards use the lighter powder blue (`--ds-inset` #EEF4FA). Accent: navy/blue for actions and highlights. Grey text is `--muted` #56647C (at least 4.5:1 on the background). Dark mode keeps its own palette. |

## Adding something new
- New box on a page → use `.card`. New box inside a card → give it the inset look (rule 2). Don't add borders.
- New button → use one of the button classes above. Don't style a button from scratch.
- Show state with `.badge` plus a status class (`ALLOW`, `ESCALATE`, `DENY`, `OPEN`, `PASS`, …), not with border colours.
