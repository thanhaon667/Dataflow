export const meta = {
  name: 'build-feature',
  description: 'Builder + independent Reviewer/Auditor loop: implement one feature end-to-end in this project, verify independently, fix, repeat until approved (max 2 rounds)',
  whenToUse: 'Use for any new feature request in this project (C:/Users/AKIBA/Desktop/python) where the user wants it built and self-verified with minimal back-and-forth. Pass the feature requirement as args (a string, or an object {requirement, notes}).',
  phases: [
    { title: 'Build', detail: 'Implement the requested feature' },
    { title: 'Review', detail: 'Independent functional review and testing' },
    { title: 'Audit', detail: 'Independent quality/security audit' },
    { title: 'Fix', detail: 'Builder addresses confirmed issues' },
  ],
}

// ---------------------------------------------------------------------------
// Input
// ---------------------------------------------------------------------------
const requirement = typeof args === 'string' ? args : (args && args.requirement) || ''
const notes = (args && typeof args === 'object' && args.notes) || ''

if (!requirement) {
  throw new Error('build-feature workflow needs args.requirement (or a plain string args) describing what to build.')
}

// ---------------------------------------------------------------------------
// Shared project context every agent gets, so each one is self-sufficient
// ---------------------------------------------------------------------------
const PROJECT_CONTEXT = `Project root: C:/Users/AKIBA/Desktop/python — a PostgreSQL + Python ERP system ("erp_support" database): Customer Support tickets + a Lead-to-Sale pipeline, plus a "Script Center" control dashboard. Read C:/Users/AKIBA/Desktop/python/README.md first for the full picture before doing anything else.

Conventions already established in this codebase — follow them, don't invent a different style:
- erp/config.py loads all settings from .env via python-dotenv (override=True) with safe defaults; any new external integration (email, API, etc.) follows the same "log a warning and no-op if unconfigured, never crash the caller" fallback philosophy already used by erp/ai_client.py, erp/clickup_client.py, and erp/emailer.py.
- erp/db.py exposes a SQLAlchemy \`engine\` already connected to the live database.
- Visual design language (used by erp/html_report.py and dashboard/script_center.py — read either for the exact values): palette INK #1c1d22, INK_DIM #6b6f76, BLUE #3452eb, CORAL #ff5a36, GREEN #12b886, VIOLET #7c5cff, AMBER #f2b705, RED #e0393e, background #f5f3ef, surface #ffffff, line #e7e2d8. Fonts: 'Fraunces' (serif, headings/big numbers), 'Public Sans' (body), 'IBM Plex Mono' (labels/timestamps/code) via Google Fonts. Any new user-facing page should match this look, not default unstyled widgets.
- venv at C:/Users/AKIBA/Desktop/python/venv (python.exe at venv/Scripts/python.exe) already has streamlit, pandas, SQLAlchemy, plotly, matplotlib, requests, psycopg2-binary, fastapi, uvicorn installed. Prefer stdlib or already-installed libraries over adding new dependencies; if you must add one, say why in your summary.
- New standalone scripts follow the existing pattern: a module docstring with a one-line purpose + a "Run:" usage example, logging via the \`logging\` module (not bare print, except for final human-readable summaries), and a \`.bat\` launcher in the project root matching the style of run_dashboard.bat / run_script_center.bat when the feature is user-launched.
- To start a Streamlit/FastAPI server on Windows and test it without hanging: PowerShell \`Start-Process -FilePath "venv\\Scripts\\python.exe" -ArgumentList ... -RedirectStandardOutput "x.log" -RedirectStandardError "y.log" -WindowStyle Hidden\`, wait a few seconds, hit it with \`Invoke-WebRequest\`, check the log files, then stop it via \`Get-NetTCPConnection -LocalPort <port>\` + \`Stop-Process\` before finishing — never leave an orphaned server running, and delete scratch log files you created purely for testing.
- DATA FLOW MAP (keep it truthful): the ERP Desk "Data Flow" page (desktop/flow_definition.py: NODES, EDGES incl. each automated edge's armed_by, LANES, TRACE; live numbers in desktop/flow_data.py; UI in desktop/static/flow.js/flow.css) is a HAND-DECLARED map of how data moves and what triggers each step. It does NOT discover new features by itself. Whenever your feature adds, removes or changes a script, pipeline stage, database table/view, external integration, output/report, or an automation trigger (webhook, Task Scheduler job, background loop), you MUST update desktop/flow_definition.py in the same change - correct file paths, data in/out, trigger type, an armed_by for every automated edge, and live metrics if a real count/last-run exists - and confirm the Data Flow page still loads and shows it (no 'unknown' edges, no unmapped-script warning). If the feature genuinely does not touch the flow, say so explicitly in your summary.
- Never commit or print real secrets; .env is gitignored and stays that way.`

function builderPrompt(task, priorFeedback) {
  return `You are the BUILDER agent. ${PROJECT_CONTEXT}

FEATURE REQUIREMENT (from the project owner, verbatim):
"""
${task}
"""
${notes ? `\nAdditional notes: ${notes}\n` : ''}
${priorFeedback ? `\nAn independent Reviewer and Auditor previously found these confirmed issues in your implementation — read the current file state first (don't assume your earlier code is still exactly as you left it), then fix all of them for real:\n${JSON.stringify(priorFeedback, null, 2)}\n` : ''}
Design and implement this completely — you decide the concrete architecture, file layout, and UX details the requirement doesn't spell out, using good judgment and the existing codebase's conventions. Don't just scaffold: write working code, then actually run/start what you built and confirm it behaves correctly before declaring done (see the testing pattern in the shared context above). Update README.md with a short section documenting the new feature (what it does, how to launch it) if it's user-facing.

Return a structured summary: files created, files modified, a description of what you built and why you made the key decisions you did, exactly how to run/use it, and any known limitations or things you deliberately simplified.`
}

function reviewPrompt(build) {
  return `You are the REVIEWER agent — independent from whoever built this. Do not assume the Builder's self-report is accurate; verify by reading the real files and actually running things yourself. ${PROJECT_CONTEXT}

ORIGINAL FEATURE REQUIREMENT:
"""
${requirement}
"""

The Builder reports having done this:
${JSON.stringify(build, null, 2)}

Independently confirm: (1) it actually runs/works as claimed — start it, exercise it, read the output, don't just read the code and assume; (2) it genuinely satisfies the requirement above, not just the Builder's interpretation of it; (3) it doesn't break anything that worked before (spot-check adjacent existing functionality if the change touches shared files); (4) if the feature adds or changes a script, pipeline stage, table, integration, report or automation trigger, desktop/flow_definition.py (the Data Flow map) was updated to match and the Data Flow page still loads with no unmapped-script warning and no 'unknown' edges - report a missing/incorrect map update as a 'major' issue. Report concrete issues with a severity each ('blocking' = doesn't work / crashes, 'major' = works but clearly violates the requirement, 'minor'/'nit' = polish), what you actually tested, and an overall pass/fail verdict. Clean up any test servers/processes/scratch files you started.`
}

function auditPrompt(build) {
  return `You are the AUDITOR agent — a second, independent pass focused on what a functional reviewer might miss: code quality, security, maintainability, edge cases, and completeness against the original ask. You are not the Builder or the Reviewer; read the actual code yourself rather than trusting either one's summary. ${PROJECT_CONTEXT}

ORIGINAL FEATURE REQUIREMENT:
"""
${requirement}
"""

The Builder reports having done this:
${JSON.stringify(build, null, 2)}

Look especially for: security issues (input validation, path handling, injection, secrets handling), error handling gaps (what happens on a bad/missing dependency, empty data, or an unattended/scheduled run with nobody watching the console), and anything in the original requirement that's missing, only partially done, or technically present but not actually usable the way a non-technical user would expect. Form your concerns, then re-read the specific code for each one to confirm it's real (not a misunderstanding) before reporting it. Return your concerns with severities and your reasoning/discussion notes for each, especially anything security-related.`
}

// ---------------------------------------------------------------------------
// Run: Build -> (Review + Audit -> Fix if needed) x up to 2 rounds
// ---------------------------------------------------------------------------
const BUILD_SCHEMA = {
  type: 'object',
  properties: {
    files_created: { type: 'array', items: { type: 'string' } },
    files_modified: { type: 'array', items: { type: 'string' } },
    summary: { type: 'string' },
    how_to_run: { type: 'string' },
    known_limitations: { type: 'string' },
  },
  required: ['summary', 'how_to_run'],
}

const ISSUE_ITEM = {
  type: 'object',
  properties: {
    description: { type: 'string' },
    severity: { type: 'string', enum: ['blocking', 'major', 'minor', 'nit'] },
    file: { type: 'string' },
  },
  required: ['description', 'severity'],
}

const REVIEW_SCHEMA = {
  type: 'object',
  properties: {
    tested: { type: 'string' },
    issues: { type: 'array', items: ISSUE_ITEM },
    verdict: { type: 'string', enum: ['pass', 'fail'] },
  },
  required: ['tested', 'issues', 'verdict'],
}

const AUDIT_SCHEMA = {
  type: 'object',
  properties: {
    concerns: { type: 'array', items: ISSUE_ITEM },
    discussion_notes: { type: 'string' },
  },
  required: ['concerns', 'discussion_notes'],
}

phase('Build')
log('Builder is designing and implementing the feature...')
let build = await agent(builderPrompt(requirement, null), { label: 'builder', schema: BUILD_SCHEMA, effort: 'high' })

// An agent() call returns null if the subagent dies (e.g. usage limit, API error).
// Retry once under a new label; never treat a missing review/audit as an approval.
async function agentWithRetry(prompt, opts) {
  const first = await agent(prompt, opts)
  if (first) return first
  log(`${opts.label} returned nothing - retrying once...`)
  return await agent(prompt, { ...opts, label: `${opts.label}-retry` })
}

if (!build) {
  return { approved: false, error: 'Builder agent failed (no result) even though nothing was reviewed.', rounds_used: 0 }
}

let round = 0
let approved = false
let incomplete = null
let lastReview = null
let lastAudit = null

while (round < 2 && !approved) {
  phase('Review')
  log(`Round ${round + 1}: independent functional review...`)
  lastReview = await agentWithRetry(reviewPrompt(build), { label: `reviewer-${round + 1}`, schema: REVIEW_SCHEMA })

  phase('Audit')
  log(`Round ${round + 1}: independent quality/security audit...`)
  lastAudit = await agentWithRetry(auditPrompt(build), { label: `auditor-${round + 1}`, schema: AUDIT_SCHEMA, effort: 'high' })

  if (!lastReview || !lastAudit) {
    incomplete = `Round ${round + 1}: ${!lastReview ? 'reviewer' : 'auditor'} agent failed twice - result NOT independently verified.`
    log(incomplete)
    break
  }

  const blocking = [...(lastReview.issues || []), ...(lastAudit.concerns || [])]
    .filter(i => i.severity === 'blocking' || i.severity === 'major')

  if (blocking.length === 0) {
    approved = true
    break
  }

  log(`${blocking.length} blocking/major issue(s) found — sending back to Builder to fix (round ${round + 1})...`)
  phase('Fix')
  const fixed = await agentWithRetry(builderPrompt(requirement, blocking), { label: `builder-fix-${round + 1}`, schema: BUILD_SCHEMA, effort: 'high' })
  if (!fixed) {
    incomplete = `Round ${round + 1}: the fix agent failed twice - confirmed issues may still be unfixed.`
    log(incomplete)
    break
  }
  build = fixed
  round++
}

return {
  approved,
  incomplete,
  rounds_used: round + 1,
  final_build: build,
  last_review: lastReview,
  last_audit: lastAudit,
}
