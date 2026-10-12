#!/usr/bin/env bash
# Run a command with a scrubbed environment so the sandboxed cmux and its Claudes
# never inherit the caller's variables (cmux-tui hands new terminals the env of
# whichever client created them). Claude gets its own config dir, so hooks and
# settings never touch your real ~/.claude.
CMUXP_HOME="${CMUXP_HOME:-$HOME/.cmuxp-sandbox}"
mkdir -p "$CMUXP_HOME/claude-config"
args=(HOME="$HOME" TERM="${TERM:-xterm-256color}" LANG="${LANG:-en_US.UTF-8}"
      PATH="${SPIKE_PATH:-$PATH}" CLAUDE_CONFIG_DIR="$CMUXP_HOME/claude-config"
      CMUXP_HOME="$CMUXP_HOME" SPIKE_PATH="${SPIKE_PATH:-$PATH}")
# Pass through only what is set: identity, temp dir (cmux-tui keeps its socket
# there), proxy/CA settings, and optional non-interactive Claude credentials.
for v in USER LOGNAME SHELL TMPDIR COLORTERM \
         HTTPS_PROXY https_proxy HTTP_PROXY http_proxy NO_PROXY no_proxy \
         NODE_EXTRA_CA_CERTS SSL_CERT_FILE ANTHROPIC_BASE_URL ANTHROPIC_API_KEY \
         CLAUDE_CODE_OAUTH_TOKEN; do
  [ -n "${!v:-}" ] && args+=("$v=${!v}")
done
exec env -i "${args[@]}" "$@"
