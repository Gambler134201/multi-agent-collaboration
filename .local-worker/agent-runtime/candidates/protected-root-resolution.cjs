'use strict';
/**
 * CANDIDATE — NOT WIRED. Nothing requires this module.
 *
 * The native guards' revision digest hashes only workbuddy-native-guard.cjs,
 * zcode-native-guard.cjs, record-policy.cjs and endpoint-access.cjs, so the
 * existence of this file alone changes no shipped behaviour and binds no
 * session. It is a decision proposal, not an active boundary.
 *
 * Gap it addresses (recorded in
 * .local-worker/tests/test_native_profile_boundary.cjs as "documents current
 * lexical boundary: junction alias to the original tree is only caught on
 * resolved file targets"): workbuddy-native-guard.cjs matches protected roots
 * in shell command text purely lexically (referencesProtectedRoot). A
 * junction/symlink alias pointing at a protected root never spells the root,
 * so terminal commands such as `type "<alias>\secret.txt"` or
 * `Remove-Item "<alias>" -Recurse` pass the terminal protected-root check.
 * For the isolated-edit job shape (protected root = record_project) the
 * separate endpoint-access isolation layer still resolves aliases through
 * realpathDeep and refuses (workspace_isolation); protected roots that are
 * not the record project (other protected_roots entries) currently have no
 * second layer on the terminal path.
 *
 * Judgement (referencesProtectedRootResolved(command, root, base)):
 *   1. Tokenise the command text with the same token shape record-policy.cjs
 *      uses for terminal analysis. extractTokens and looksLikeTarget are
 *      private there, so equivalent bodies are mirrored verbatim below; if
 *      this candidate is wired, prefer exporting them from record-policy.cjs
 *      and deleting the mirrors instead of keeping two copies.
 *   2. Skip tokens carrying shell expansion syntax (`$`, `%`, backtick):
 *      their target cannot be proven from the text. This is deliberately a
 *      skip, not a refusal — see the trade-off note below. Recursive cleanup
 *      with variables stays covered by the guard's
 *      unverified_recursive_cleanup rule.
 *   3. Resolve every remaining path-like token to an absolute path with
 *      policy.resolveAgainst against `base`, then resolve junctions/symlinks
 *      with policy.realpathDeep. Its longest-existing-prefix fallback also
 *      covers wildcard leaves (`<alias>\*`) and not-yet-existing files.
 *   4. Match the boundary after policy.normalizePath: a hit is a result equal
 *      to the root or starting with root + path.sep. Same-prefix siblings such
 *      as `<root>-task-workspaces` or `<root>-backup` never match. Both the
 *      spelled root and realpathDeep(root) are accepted as the boundary so a
 *      root declared through an alias still protects the physical tree.
 *
 * `base` is the shell working directory used for relative tokens. A caller
 * that wires this must pass the same value the guard's leading-location
 * analysis uses (args.workdir || args.cwd || job.project); with a different
 * base, relative spellings judge the wrong location. When omitted, the
 * protected root's parent directory is the default: absolute tokens — the
 * common case — ignore base entirely, and sibling aliases typically live
 * beside the protected root, so bare alias names still resolve there.
 * process.cwd() is only a last-resort fallback for a degenerate root.
 *
 * Correspondence with the lexical matcher (workbuddy-native-guard.cjs
 * referencesProtectedRoot). This module deliberately does not import or
 * reimplement the matcher; the guard file is a permission boundary and must
 * stay free of candidate code. The two views differ in both directions:
 *   - caught here, invisible lexically: junction/symlink alias spellings and
 *     relative tokens that resolve into the root;
 *   - caught lexically, invisible here: the root spelled inside a larger
 *     token (switch values like `--path=<root>`, code strings like
 *     `open('<root>\f')`), prose sentences containing the path, trailing-dot
 *     spellings (`<root>.`), and alternate-data-stream spellings
 *     (`<root>:stream`) — the lexical matcher sees raw text, this module only
 *     judges tokens that resolve, and Win32 realpath rejects those forms;
 *   - therefore, if wired, this check must be ADDED BESIDE the lexical
 *     matcher, never replace it.
 *
 * Failure stance (decision point, kept conservative): the function never
 * throws. An empty or unreadable root, a non-string command, or any
 * unexpected error during resolution returns true — "cannot prove safe,
 * refuse" (fail closed). Compatibility impact of that stance: a defect or an
 * exotic environment in the resolver would deny terminal commands rather than
 * allow them; it can never open access, but it could block legitimate work
 * until fixed, and wired deployments should watch denial_rule
 * 'native_profile' rates after rollout. The narrower choice — expansion
 * tokens are skipped instead of refused — keeps ordinary variable commands
 * (`Write-Output "$env:USERNAME"`) allowed exactly as today; a stricter
 * variant refusing on every unresolvable token would deny large classes of
 * normal commands and is not implemented here.
 *
 * Pure decision module: no writes, no network, no retries, no logging of
 * command text or matched values.
 */

const path = require('path');
const policy = require('../record-policy.cjs');

/** Shell expansion syntax: the target is computed at run time, not from text. */
const UNRESOLVABLE = /[$%`]/;

/** Verbatim mirror of record-policy.cjs stripQuotes (private there). */
function stripQuotes(value) {
  let text = String(value == null ? '' : value).trim();
  if (text.length >= 2
      && ((text[0] === '"' && text[text.length - 1] === '"')
          || (text[0] === "'" && text[text.length - 1] === "'"))) {
    text = text.slice(1, -1).trim();
  }
  return text;
}

/** Verbatim mirror of record-policy.cjs extractTokens (private there). */
function extractTokens(command) {
  const tokens = [];
  const quoted = /"([^"]*)"|'([^']*)'/g;
  let match = quoted.exec(command);
  while (match !== null) {
    const value = match[1] !== undefined ? match[1] : match[2];
    if (value && value.trim()) tokens.push(value.trim());
    match = quoted.exec(command);
  }
  for (const raw of String(command).split(/\s+/)) {
    const token = raw.replace(/^[|&;()<>]+/, '').replace(/[|&;()<>]+$/, '');
    if (token) tokens.push(token);
  }
  return tokens;
}

/** Verbatim mirror of record-policy.cjs looksLikeTarget (private there). */
function looksLikeTarget(token) {
  const text = stripQuotes(token);
  if (!text) return false;
  if (text.startsWith('-')) return false;
  if (/^\/[A-Za-z]{1,3}$/.test(text)) return false;
  if (/[|&;<>]+/.test(text)) return false;
  return true;
}

/** record-policy.cjs isEqualOrInside semantics with the platform separator. */
function isEqualOrInside(value, root) {
  return value === root || value.startsWith(root + path.sep);
}

/** The protected root's parent, the default shell cwd when none is given. */
function defaultBase(root) {
  const parent = path.dirname(path.resolve(root));
  return parent && parent !== '.' ? parent : process.cwd();
}

/**
 * True when some path-like token of `command` resolves (through junctions or
 * symlinks) to the protected `root` or inside it. Never throws; see the
 * module docstring for the full semantics and the fail-closed trade-off.
 * @param {string} command terminal command text
 * @param {string} root absolute protected root
 * @param {string} [base] shell working directory for relative tokens
 * @returns {boolean}
 */
function referencesProtectedRootResolved(command, root, base) {
  try {
    if (typeof root !== 'string') return true; // cannot prove → refuse
    const rootNorm = policy.normalizePath(root);
    if (!rootNorm) return true; // mirrors the lexical matcher's empty-root rule
    if (typeof command !== 'string') return true; // unprovable input → refuse
    const workBase = typeof base === 'string' && base.trim() ? base : defaultBase(root);
    const boundaries = [rootNorm];
    const rootReal = policy.normalizePath(policy.realpathDeep(root));
    if (rootReal && !boundaries.includes(rootReal)) boundaries.push(rootReal);
    for (const token of extractTokens(command)) {
      if (!looksLikeTarget(token)) continue;
      if (UNRESOLVABLE.test(token)) continue;
      const resolved = policy.resolveAgainst(workBase, token);
      if (!resolved) continue;
      if (boundaries.some((boundary) => isEqualOrInside(policy.normalizePath(resolved), boundary))) return true;
      const real = policy.normalizePath(policy.realpathDeep(resolved));
      if (boundaries.some((boundary) => isEqualOrInside(real, boundary))) return true;
    }
    return false;
  } catch (_) {
    return true; // resolution failed: cannot prove safe → refuse (fail closed)
  }
}

module.exports = { referencesProtectedRootResolved };
