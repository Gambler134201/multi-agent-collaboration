'use strict';
/**
 * CANDIDATE — NOT WIRED. Nothing requires this module.
 *
 * The native guards' revision digests hash only workbuddy-native-guard.cjs,
 * zcode-native-guard.cjs, record-policy.cjs and endpoint-access.cjs, and the
 * DSH plugin / Pi extension revisions hash their own fixed file sets. The
 * existence of this file alone changes no shipped behaviour and binds no
 * session. It is a decision proposal for the "mature shell parser" seam, not
 * an active boundary.
 *
 * Problem it addresses (docs/SHELL_GUARD.md 2026-10-26 "接线前提" and the P4
 * handover): the shared record policy judges terminal command text with
 * dialect-agnostic lexical heuristics, and `workbuddy-native-guard.cjs`
 * currently derives a tool-name argument (`Bash`→`bash`, `PowerShell`→`pwsh`)
 * that `record-policy.cjs` only uses for tool matching — never for dialect
 * selection. A mature parser (tree-sitter-bash, a PowerShell AST) may only be
 * wired for a dialect that is *proven* for that executor/tool call, and
 * "unknown" must stay unknown: the P4 handover forbids substituting tool
 * names, environment variables, or locally installed programs for the actual
 * selection evidence. This module encodes exactly the evidence this batch
 * verified by read-only inspection of the installed, version-pinned programs.
 *
 * Evidence levels, strongest first:
 *   per_call_receipt    a field bound to this exact tool call states the
 *                       dialect/shell. No host offers one today (verified:
 *                       DSH exec object, Pi tool_call event, WorkBuddy/ZCode
 *                       hook input JSON, and every tool-result payload each
 *                       host documents).
 *   pinned_construction the pinned version's own install code fixes the
 *                       interpreter for this tool name by construction
 *                       (hardcoded argv, or a gate that must have held for
 *                       the call to exist). Dialect-class evidence only:
 *                       the concrete executable still varies per machine.
 *   ambiguous_construction the pinned code selects between dialects by an
 *                       environment-dependent condition the call payload does
 *                       not carry; per call this stays unknown.
 *   unknown             no admissible evidence; re-verify the install before
 *                       claiming anything.
 *
 * This module never reads the filesystem, the environment or the network:
 * the caller supplies the executor identity and the version it has already
 * verified through its own existing checks (dsh_worker/executor_adapter for
 * DSH, pi_worker for Pi, workbuddy_executor for WorkBuddy, zcode_worker for
 * ZCode). A version this batch did not research yields unknown even when the
 * executor itself is known — newer installs must be re-inspected, not
 * assumed.
 *
 * What a wiring would consume: `dialect` ('bash' | 'powershell' | null).
 * The parser component itself is deliberately NOT part of this candidate.
 * Wiring anything into the pinned decision files changes their revision
 * digests and invalidates in-flight sessions, so the wiring decision should
 * bundle the seam with the parser step it enables. Until then the guards'
 * behaviour stays exactly as today for every input, proven or not: nothing
 * here denies or allows a call.
 */

/** Evidence-level identifiers, strongest first. */
const LEVELS = {
  PER_CALL: 'per_call_receipt',
  CONSTRUCTION: 'pinned_construction',
  AMBIGUOUS: 'ambiguous_construction',
  UNKNOWN: 'unknown',
};

const BASH = 'bash';
const POWERSHELL = 'powershell';

/**
 * The researched evidence table. Each entry's `basis` names the installed
 * module and the exact argv/gate observed in that version's public code, so
 * a reviewer can re-derive the claim; versions name the only builds this
 * evidence was verified against.
 *
 * DSH — product 0.2.1.0 on this machine, dsh-tools 0.2.1-rc.2
 * (E:\dsh\resources\app\dsh\node_modules\@deepseek-ai\...):
 *   dsh-tool-bash registers tool `bash`; dsh-bash-local's execute() spawns
 *   argv ["bash","-c",command] through ctx.subprocess (subprocess-local
 *   resolves the bare name over PATH+PATHEXT; every resolver hit — Git Bash,
 *   MSYS2, WSL — is a bash interpreter, so the grammar is bash whenever the
 *   tool runs at all).
 *   dsh-tool-pwsh registers tool `pwsh`; dsh-pwsh-local spawns
 *   [resolvePwshPath(config.pwshPath), "-NoLogo","-NoProfile",
 *   "-NonInteractive","-Command", ENCODING_PREAMBLE+command]. resolvePwshPath:
 *   explicit config → %ProgramFiles%\PowerShell\7\pwsh.exe → PATH pwsh.exe →
 *   System32 WindowsPowerShell v1.0 powershell.exe — PowerShell grammar in
 *   every case; whether 7 or 5.1 serves a call is machine state (unknown).
 *   The guard surface (ctx.tools.guard) receives exec = {token, callId,
 *   rootCallId, name, signal, agent?, parent?, schema?, arguments} — no shell
 *   field.
 *
 * Pi — @earendil-works/pi-coding-agent 1.3.0, exact-pinned by pi_worker
 * (E:\pi\npm-global\node_modules\@earendil-works\pi-coding-agent):
 *   dist/core/tools/bash.js: createLocalBashOperations →
 *   createLocalShellOperations("bash", () => getShellConfig(shellPath));
 *   dist/utils/shell.js getShellConfig: settings shellPath →
 *   %ProgramFiles%\Git\bin\bash.exe → bash.exe on PATH (bash grammar).
 *   dist/core/tools/powershell.js: createLocalPowerShellOperations →
 *   createLocalShellOperations("PowerShell", getPowerShellConfig);
 *   getPowerShellConfig spawns findExecutableOnPath("pwsh.exe") ??
 *   findExecutableOnPath("powershell.exe") with ["-NoProfile",
 *   "-NonInteractive","-ExecutionPolicy","Bypass","-Command", utf8prefix+
 *   command] (PowerShell grammar). The extension API's tool_call event
 *   carries only {type, toolCallId, parentToolCallId?, toolName, input} —
 *   no shell metadata, and pi.getAllTools() exposes no shell path.
 *
 * WorkBuddy — @genie/workbuddy-desktop 5.8.3 (E:\workbuddy, cli bundle
 * cli/dist/codebuddy-lite-wb.mjs; 5.8.2 was NOT re-inspected for these
 * claims): the PowerShell tool is gated by
 * isPowerShellToolEnabled() = Windows && CODEBUDDY_USE_POWERSHELL_TOOL not
 * "0"/"false" && findPowerShell() !== null, so a PowerShell tool call that
 * exists implies a PowerShell runtime was found (pwsh 7 core or Windows
 * PowerShell 5.1 desktop — which one stays machine state). The host's own
 * sandbox mapping derives `dialect: toolName==="powershell" ? "powershell" :
 * "posix"` from the name alone. The Bash tool, however, is ambiguous by
 * construction: getShellConfiguration() returns findBashPath() bash `-c`
 * when Git Bash is found, and otherwise silently falls back to the
 * PowerShell configuration ("Git Bash not found, shell falls back to
 * PowerShell"); the hook payload cannot show which branch served a call.
 * workbuddy_executor.py strips CODEBUDDY_* / WORKBUDDY_* from the child env,
 * so our sessions always use this default discovery.
 *
 * ZCode — @zcode/desktop 3.16.3, exact-pinned by zcode_worker: recorded as
 * unknown. The Bash tool's dialect is resolved once per session from the
 * desktop setting `integratedTerminalShell` (~/.zcode/v2/setting.json,
 * absent = auto) and Windows auto-detection (Git Bash install paths, then
 * git.exe on PATH → sibling bash.exe); when nothing is found the tool falls
 * back to the legacy shell (cmd.exe via ComSpec) — so the tool name proves
 * nothing. The hook input JSON (cwd, hookEventName, mode, riskLevel,
 * sideEffectScope, sessionId, timestamp, toolCallId, toolInput, toolName,
 * traceId, turnId and snake_case aliases) carries no shell field. The
 * admissible future evidence is session-scoped, not per-call: the resolved
 * selection is memoized for the session and readable pre-prompt through the
 * desktop service surface (settingService.get().integratedTerminalShell,
 * systemService.listIntegratedTerminalShells()) that the existing CDP bridge
 * could extend; a wiring would record it in the task record instead of
 * guessing. PowerShell is not a possible agent Bash dialect on Windows (it
 * only serves the integrated-terminal pty, which tasks never use).
 */
const EVIDENCE = {
  dsh: {
    versionMatch: (version) => typeof version === 'string' && version.startsWith('0.2.1'),
    tools: {
      bash: {
        dialect: BASH,
        level: LEVELS.CONSTRUCTION,
        basis: 'dsh-bash-local (dsh-tools 0.2.1-rc.2) spawns ["bash","-c",command]; the bare name resolves over PATH+PATHEXT and every hit is a bash interpreter',
      },
      pwsh: {
        dialect: POWERSHELL,
        level: LEVELS.CONSTRUCTION,
        basis: 'dsh-pwsh-local (dsh-tools 0.2.1-rc.2) spawns [resolvePwshPath(config),"-NoLogo","-NoProfile","-NonInteractive","-Command",preamble+command]; config → PS7 → PATH pwsh → Windows PowerShell 5.1 are all PowerShell',
      },
    },
    limits: 'which bash executable resolves, and pwsh 7 vs 5.1, are machine state; WSL bash would resolve Linux-side paths, so relative-path semantics stay a separate unknown; the existing DSH version resolver reads only resources/app.asar and reports nothing on expanded app installs',
  },
  pi: {
    versionMatch: (version) => version === '1.3.0',
    tools: {
      bash: {
        dialect: BASH,
        level: LEVELS.CONSTRUCTION,
        basis: 'pi 1.3.0 createLocalShellOperations("bash", getShellConfig(shellPath)) spawns the resolved bash with ["-c",command]: settings shellPath → Git Bash under Program Files → bash.exe on PATH',
      },
      powershell: {
        dialect: POWERSHELL,
        level: LEVELS.CONSTRUCTION,
        basis: 'pi 1.3.0 createLocalShellOperations("PowerShell", getPowerShellConfig) spawns pwsh.exe ?? powershell.exe on PATH with ["-NoProfile","-NonInteractive","-ExecutionPolicy","-Command",utf8prefix+command] — always PowerShell',
      },
    },
    limits: 'which bash/powershell executable resolves is machine state; pi_worker pins the package version exactly before dispatch',
  },
  workbuddy: {
    versionMatch: (version) => version === '5.8.3',
    tools: {
      PowerShell: {
        dialect: POWERSHELL,
        level: LEVELS.CONSTRUCTION,
        basis: 'WorkBuddy 5.8.3 gates the PowerShell tool on findPowerShell() !== null (CODEBUDDY_POWERSHELL_PATH → pwsh on PATH → powershell on PATH → PS7 in Program Files → System32 5.1) and spawns it with -NoProfile -NonInteractive -Command; a call that exists implies a PowerShell runtime was found',
      },
      Bash: {
        dialect: null,
        level: LEVELS.AMBIGUOUS,
        basis: 'WorkBuddy 5.8.3 getShellConfiguration: findBashPath() bash -c when Git Bash is found, otherwise the Bash tool silently falls back to the PowerShell configuration; the hook payload carries no field distinguishing the branch',
      },
    },
    limits: 'no per-call receipt exists in hook input, tool results or session events; the PowerShell tool call proves a PowerShell runtime but not its version/edition; 5.8.2 must be re-inspected before claiming the same entries',
  },
  zcode: {
    versionMatch: (version) => version === '3.16.3',
    tools: {},
    limits: 'the Bash tool resolves git-bash or the cmd legacy shell once per session from the desktop setting and Git Bash detection; no hook, result or event field exposes the choice, and it is not bound to a tool name here — the designated future evidence is the session-scoped desktop service readback (settingService/systemService) recorded in the task record before the prompt, not a tool-name guess',
  },
};

const UNKNOWN_RESULT = Object.freeze({ dialect: null, level: LEVELS.UNKNOWN, basis: null });

/**
 * Resolve the terminal dialect evidence for one executor/tool pair.
 * @param {string} executor executor identity ('dsh' | 'pi' | 'workbuddy' | 'zcode')
 * @param {string} toolName tool name exactly as the host surfaced it
 * @param {string} [version] the version the caller already verified; a
 *   version outside the researched set degrades the answer to unknown
 * @returns {{dialect: 'bash'|'powershell'|null, level: string, basis: string|null, limits?: string}}
 */
function dialectFor(executor, toolName, version) {
  const entry = EVIDENCE[executor];
  if (!entry || typeof toolName !== 'string' || !entry.versionMatch(version)) return UNKNOWN_RESULT;
  const tool = entry.tools[toolName];
  if (!tool) return UNKNOWN_RESULT;
  return { ...tool, limits: entry.limits };
}

module.exports = { LEVELS, BASH, POWERSHELL, EVIDENCE, dialectFor };
