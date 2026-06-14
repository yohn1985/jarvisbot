"""Phase 0 bootstrap: pure-Python landing/setup that runs BEFORE Jarvis has an AI brain.

Nothing here may call an LLM (chicken-and-egg: it can't think until it has a brain). This
package holds the deterministic setup steps: preflight + dashboard-asks now; the dependency
installer, credential intake, self-scheduling, and network discovery come in later steps.
"""
