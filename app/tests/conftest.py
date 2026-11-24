"""Suite-wide test setup."""

import os

# The unit tests stub every LLM call, but a few code paths check that an API key is configured
# before they reach the stub. A dummy key lets the suite pass in a clean checkout (CI, a fresh
# clone) that has no secrets.env. A key already exported in the shell is left as it is.
os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")
