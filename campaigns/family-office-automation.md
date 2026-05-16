---
slug: family-office-automation
name: Family Office Automation (US+UK)
status: active
target_icp: Non-technical principals, founders, and COOs/Heads of Ops at single- or multi-family offices in the US and UK. Office runs investments across some mix of public markets, alternatives, direct deals, and real estate. Currently doing manual or spreadsheet-heavy work on deal flow, portfolio monitoring, investment memos, or LP/family-stakeholder reporting.
# --- ICP override: family office principals ARE investors by role, so we cannot
# use the default investor-exclusion noise filter. We exclude only the truly
# noisy signals (coaches, consultants, advisors, recruiters).
icp_role_required: "founder|co-?founder|cofounder|ceo|chief executive|president|principal|managing\s+director|chief\s+investment|cio|head\s+of\s+(family|investment|ops|operations)|family\s+office"
icp_role_excluded: "career\s+coach|life\s+coach|business\s+coach|sales\s+consultant|fundraise\s+consultant|recruiter|talent\s+acquisition|headhunter"
icp_geo_required: "United States|United Kingdom|\bUSA?\b|, CA\b|, NY\b|, MA\b|, TX\b|, IL\b|, FL\b|, CT\b|, PA\b|, GA\b|, WA\b|, CO\b|, DC\b|\bNew York\b|\bSan Francisco\b|\bLos Angeles\b|\bBay Area\b|\bSilicon Valley\b|\bBoston\b|\bGreenwich\b|\bMiami\b|\bPhiladelphia\b|\bChicago\b|\bSeattle\b|\bAustin\b|\bDenver\b|\bAtlanta\b|\bHouston\b|\bDallas\b|\bWashington\b|\bConnecticut\b|\bLondon\b|\bUK\b|\bManchester\b|\bEdinburgh\b|\bBirmingham\b|Greater London"
---

# Pitch

Cortivo is a small AI-engineering studio. Founders: Haque Farazul (ex-Mastercard PM, IIT Kharagpur top 0.5%, 5+ years shipping production AI) and Ritik Kansal (ex-Amazon SDE, runs Coding Sphere). Team includes engineers from the IITs.

We build internal AI tooling for investment firms — deal-flow agents, portfolio-monitoring systems, automated investment memos, and LP/stakeholder reporting. We pair one senior engineer with AI tooling so a family office gets a production-grade internal tool without hiring an in-house technical team. Typical engagement: 4-8 weeks for a focused workflow, ongoing thereafter.

# Approach — discovery first, propose second

This is a soft-touch campaign. Family office principals get pitched constantly and the move is NOT to lead with a service pitch. Instead:

- Connect note: reference one specific thing they've posted or said, ask an open-ended question about how their office handles a relevant workflow. Do not pitch.
- DM1 after connection: confirm what we've built for similar firms (Bespoke wealth-manager copilot, Microforge agentic deal-sourcing used by a16z/Sequoia), then ask a specific discovery question. Don't propose a project yet.
- DM2 if discovery surfaces a real pain: propose a tight scoped engagement.

# Pain points we address

- Deal flow is still managed in spreadsheets, inboxes, and CRMs that don't talk to each other — quality opportunities get missed
- Portfolio monitoring is manual: someone reads news + financial data + reports and assembles a digest. Misses time-sensitive signals.
- Investment memos take hours to draft and follow inconsistent templates; analysts re-do the same research per deal
- LP / family-stakeholder updates eat 2-3 days a quarter; manual aggregation from custodians, GPs, and operating partners
- Limited or zero in-house engineering — hiring a senior engineer takes 3+ months and isn't justified by one workflow

# Proof points (use whichever ties most cleanly to the prospect)

- **Bespoke** (Haque, GenAI engineer) — built the AI copilot for wealth managers: agents, memory, RAG, and the early-signal-detection system tying market data to actionable insights. Closest analogue to family-office portfolio monitoring.
- **Microforge** — agentic platform we built that surfaces high-potential open source companies before they raise. Used by a16z, Sequoia, Elevation Capital, and Accel for deal sourcing. Direct fit for "deal flow automation."
- **Conversational AI Chatbot for an FMCG Client** — RAG + Re-Ranking + SQL agent for financial KPI queries across structured and unstructured data. Same shape as investment-memo and stakeholder-Q&A workflows.
- **Automated Stock Transaction Extraction for a Bank** — multi-agent document intelligence pipeline that extracts trades + metadata from complex financial PDFs (Azure Document Intelligence + GPT-4 + multi-agent validation). Direct fit for ingesting custodian statements / fund reports for LP reporting.
- **Mastercard** — Haque was Product Manager for 1.5 years; speaks the fintech / enterprise dialect natively.

# Anti-claims — DO NOT say

- "Save money" / "reduce headcount" — wrong vocabulary for this audience; they value time + decision quality, not cost-cutting
- "Disrupt your workflow" / move-fast-and-break-things — they're conservative; existing process works, we augment it
- "We'll handle your sensitive data" — confidentiality is assumed, not advertised
- "Generic AI" / "ChatGPT for finance" — too commoditized; we position as bespoke engineering
- "I'd love to chat" / "open to a quick call" — banal CTAs they hear daily
- Never claim partnerships, certifications, or regulatory affiliations we don't have
- If their public content has nothing specific (just career updates, generic posts), return INSUFFICIENT_CONTEXT — don't force a pitch on thin signal

# Tone

Operator-to-operator. Concise, respectful of their time. Reference one specific thing they've written or done; one specific thing we've built; one open-ended question. Match their register — most family office principals write measured, occasionally dry posts. Don't be spiky. Don't be a salesperson. Be the engineer they'd actually want to hire.
