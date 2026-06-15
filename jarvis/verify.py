"""Self-verification — the deep-fix discipline distilled: never trust a conclusion until it has
SURVIVED an adversarial check. verify() spawns a red-team agent whose only job is to DISPROVE a
claim (not confirm it); if it breaks, the caller revises and re-checks. Outcomes are recorded as
lessons (feedback) so Jarvis learns. This is what makes shallow/confident-but-wrong answers reliable.

    v = verify(cfg, claim, context)         # -> {"survives": bool, "verified": bool, "critique": str}

FAIL-CLOSED: if the check can't actually run (no brain, error, or no explicit verdict), survives is
False and verified is False — a caller must never read an *un-run* check as a *passed* one.
"""
from __future__ import annotations


def verify(cfg, claim, context="", timeout=150):
    """Spawn a red-team agent to try to break `claim`. Returns {survives, verified, critique}:
      - survives=True,  verified=True  -> red-team genuinely could not break it
      - survives=False, verified=True  -> red-team broke it (a real defect)
      - survives=False, verified=False -> the check could not be performed (NOT verified)
    """
    from jarvis.adapters.llm import build_llm
    llm = build_llm(cfg)
    if not llm:
        return {"survives": False, "verified": False, "critique": "(no brain available to verify)"}
    prompt = ("You are a RED-TEAM verifier. Try hard to DISPROVE the claim below — find errors, "
              "unproven assumptions, missing evidence, or counter-examples. Do NOT look for support; "
              "attack it. If you cannot break it with the given context, say what evidence would settle "
              "it. End with EXACTLY one line: 'VERDICT: survives' or 'VERDICT: broken: <reason>'.\n\n"
              f"CLAIM:\n{claim}\n\nCONTEXT:\n{(context or '')[:6000]}")
    try:
        out = llm.run("red_team", prompt, timeout=timeout).strip()
    except Exception as e:
        return {"survives": False, "verified": False, "critique": f"(verify failed: {str(e)[:80]})"}
    low = out.lower()
    if "verdict: broken" in low:
        survives, verified = False, True
    elif "verdict: survives" in low:
        survives, verified = True, True
    else:                                  # no explicit verdict -> we did NOT actually verify it
        survives, verified = False, False
    try:
        from jarvis import feedback
        feedback.record(cfg, kind="verify", area="red_team", summary=str(claim)[:120],
                        outcome=("broken" if (verified and not survives) else "survives" if survives else "unverified"),
                        detail=out[:500])
    except Exception:
        pass
    return {"survives": survives, "verified": verified, "critique": out}
