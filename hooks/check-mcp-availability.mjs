/**
 * hooks/check-mcp-availability.mjs
 *
 * SubagentStop hook: aborts the pipeline when agent-project-issues MCP tools
 * are unavailable during context extraction.
 *
 * Decision logic:
 *   - Only activates for the "context-extractor" agent (agent_type check).
 *   - Reads the JSONL transcript and scans for evidence of MCP failure:
 *       foundMcpError          — a get_ticket or list_comments tool result
 *                                contains "No such tool available"
 *       foundSuccessfulFetch   — a get_ticket result that is NOT an error
 *   - If foundMcpError && !foundSuccessfulFetch → block with a clear message.
 *   - All failure modes (bad stdin, unreadable transcript, wrong agent) are
 *     treated as "do not block" (fail-safe / exit 0).
 *
 * Block output: write JSON {"decision":"block","reason":"..."} to stdout, exit 0.
 * Pass: exit 0 with no stdout.
 */

import { readFileSync } from "node:fs";
import { createInterface } from "node:readline";
import { createReadStream } from "node:fs";
import process from "node:process";

const MCP_TOOL_NAMES = ["get_ticket", "list_comments"];
const ERROR_SIGNAL = "No such tool available";

async function main() {
  // --- 1. Read and parse stdin as the hook payload ---
  let payload;
  try {
    const stdinChunks = [];
    for await (const chunk of process.stdin) {
      stdinChunks.push(chunk);
    }
    const raw = Buffer.concat(stdinChunks).toString("utf8");
    payload = JSON.parse(raw);
  } catch {
    // Malformed stdin — fail-safe, do not block.
    process.exit(0);
  }

  // --- 2. Gate on agent_type containing "context-extractor" ---
  const agentType = String(payload.agent_type ?? "");
  if (!agentType.includes("context-extractor")) {
    process.exit(0);
  }

  // --- 3. Resolve the transcript path ---
  const transcriptPath =
    payload.agent_transcript_path ?? payload.transcript_path ?? null;
  if (!transcriptPath) {
    process.exit(0);
  }

  // --- 4. Read and scan the JSONL transcript ---
  let lines;
  try {
    const content = readFileSync(transcriptPath, "utf8");
    lines = content.split(/\r?\n/);
  } catch {
    // Unreadable transcript — fail-safe, do not block.
    process.exit(0);
  }

  let foundMcpError = false;
  let foundSuccessfulFetch = false;

  for (const rawLine of lines) {
    const line = rawLine.trim();
    if (!line) continue;

    let parsed;
    try {
      parsed = JSON.parse(line);
    } catch {
      // Skip malformed JSONL lines.
      continue;
    }

    // Determine if this line involves one of our MCP tool names.
    // Transcript entries can be tool_result objects or nested inside messages.
    // We use both structured field inspection and substring scan as fallback.
    const lineStr = line; // raw string for substring fallback

    const involvesMcpTool = MCP_TOOL_NAMES.some((name) => {
      // Structured: tool_name field
      if (parsed.tool_name === name) return true;
      // Structured: nested tool calls / results in content arrays
      if (Array.isArray(parsed.content)) {
        for (const item of parsed.content) {
          if (item && item.name === name) return true;
        }
      }
      // Fallback substring scan
      return lineStr.includes(`"${name}"`);
    });

    if (!involvesMcpTool) continue;

    const involvesGetTicket =
      parsed.tool_name === "get_ticket" ||
      lineStr.includes('"get_ticket"');

    // Determine if this is an error result.
    // Priority: structured is_error field > content-equals sentinel > raw-line fallback.
    // The raw-line fallback is used ONLY when there is no structured is_error field,
    // because ticket body text may contain the error string incidentally.
    let isError = false;
    if (parsed.is_error === true) {
      isError = true;
    } else if (typeof parsed.content === "string") {
      // Content is a plain string — compare trimmed to the exact error sentinel.
      // Do NOT use .includes() here: the ticket body may contain this phrase.
      if (parsed.content.trim() === ERROR_SIGNAL) {
        isError = true;
      }
    } else if (parsed.is_error == null) {
      // No structured is_error field at all: fall back to raw substring scan,
      // but only outside of known content-bearing field names.
      // We look for the error signal NOT inside a "body" or "title" field value.
      // Simplest safe heuristic: check if the line looks like an error response
      // (no nested ticket object), i.e. the content field is just the error string.
      const contentRaw = parsed.content;
      if (contentRaw === undefined || contentRaw === null) {
        // No content field — check raw line but only if no ticket-shaped JSON keys.
        const hasTicketKeys = lineStr.includes('"title"') && lineStr.includes('"body"');
        if (!hasTicketKeys && lineStr.includes(ERROR_SIGNAL)) {
          isError = true;
        }
      }
    }

    if (involvesGetTicket) {
      if (isError) {
        foundMcpError = true;
      } else {
        // Content is substantive ticket data — successful fetch.
        foundSuccessfulFetch = true;
      }
    } else {
      // list_comments or other MCP tool error
      if (isError) {
        foundMcpError = true;
      }
    }
  }

  // --- 5. Decision ---
  if (foundMcpError && !foundSuccessfulFetch) {
    const reason =
      "context-extractor: agent-project-issues MCP tools returned " +
      '"No such tool available" — the MCP is not loaded. ' +
      "Run /reload-plugins in Claude and restart the pipeline.";
    process.stdout.write(JSON.stringify({ decision: "block", reason }));
    process.exit(0);
  }

  process.exit(0);
}

main().catch(() => {
  // Any unexpected error → fail-safe, do not block.
  process.exit(0);
});
