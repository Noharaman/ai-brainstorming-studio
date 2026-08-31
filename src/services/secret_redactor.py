"""Scrubs known secret values out of text.

Deliberately has no idea where secrets come from. `ProcessRunner` redacts a
run's captured output using the exact credential that run was given, before
the CommandResult exists — so persistence under `.ai-brainstorm/`, the GUI
panes, the text fed back to LM Studio and the chat history all receive
already-clean text, and none of them has to remember to call this.

Looking the secret up here instead would reintroduce two problems: text
captured with an older token could no longer be scrubbed once the token
changed, and a display path would have to hit the Keychain on the GUI thread.
"""
from __future__ import annotations

import re

REDACTED = "***REDACTED***"

# Below this length a "secret" is more likely to be an ordinary substring, and
# blanking every occurrence would corrupt unrelated output.
MIN_REDACTABLE_LENGTH = 8

# Credential shapes, matched structurally. Existing CLI Mode inherits the
# user's API keys into the child environment, so a CLI can echo one back in an
# error message — but the app must not go *reading* environment variables to
# learn what to look for. Recognising the published prefix of a credential
# needs no access to its value, which is the difference that matters.
_STRUCTURAL_PATTERNS = (
    re.compile(r"sk-ant-[A-Za-z0-9_-]{16,}"),        # Anthropic
    re.compile(r"sk-proj-[A-Za-z0-9_-]{16,}"),       # OpenAI project keys
    re.compile(r"sk-[A-Za-z0-9]{32,}"),              # OpenAI classic
    re.compile(r"AIza[A-Za-z0-9_-]{20,}"),           # Google API keys
    re.compile(r"ya29\.[A-Za-z0-9_-]{20,}"),         # Google OAuth access token
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),       # GitHub
    re.compile(r"AKIA[0-9A-Z]{16}"),                 # AWS access key id
    re.compile(r"ASIA[0-9A-Z]{16}"),                 # AWS temporary key id
    re.compile(r"xox[abposr]-[A-Za-z0-9-]{10,}"),    # Slack
    # JWTs: three base64url segments. Test output and error messages carry
    # these routinely, and the payload is often enough to impersonate.
    re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    # PEM private key blocks, including the body: matching only the header
    # would preserve the key itself on the following lines.
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
    # Credentials inside a connection URI (postgres://user:pass@host). Only
    # the password is replaced so the rest stays readable for debugging.
    re.compile(r"(?<=://)[^\s:/@]+:[^\s:/@]+(?=@)"),
)

#: `NAME=value` where NAME looks like a credential. Deliberately narrow: it
#: keys off the *name*, so an unremarkable-looking value is still caught when
#: it is assigned to something called SECRET/TOKEN/PASSWORD. A test suite
#: echoing its environment is the common way these reach a log.
_ASSIGNMENT_PATTERN = re.compile(
    r"""(?ix)
    \b
    (?P<name>[A-Z0-9_]*
        (?:SECRET|TOKEN|PASSWORD|PASSWD|APIKEY|API_KEY|ACCESS_KEY|
           PRIVATE_KEY|CREDENTIAL|AUTH)
     [A-Z0-9_]*)
    (?P<sep>\s*[:=]\s*)
    (?P<quote>["']?)
    (?P<value>[^\s"'\n]{4,})
    (?P=quote)
    """
)


def redact(text: str, *secrets: str | None) -> str:
    """Replace known secret values and credential-shaped substrings in `text`.

    Known values are replaced first, longest first so that one containing
    another doesn't leave a fragment behind. The structural patterns then
    catch credentials this process was never told about — an API key the user
    configured themselves, echoed back by a CLI in an error message."""
    if not text:
        return text
    usable = sorted(
        {s for s in secrets if s and len(s) >= MIN_REDACTABLE_LENGTH},
        key=len,
        reverse=True,
    )
    for secret in usable:
        text = text.replace(secret, REDACTED)
    for pattern in _STRUCTURAL_PATTERNS:
        text = pattern.sub(REDACTED, text)
    # Keeps the name so the reader can still see *what* was set, which is
    # usually the useful part when debugging a failing test.
    text = _ASSIGNMENT_PATTERN.sub(
        lambda m: f"{m.group('name')}{m.group('sep')}{REDACTED}", text
    )
    return text
