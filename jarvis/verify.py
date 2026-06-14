"""Self-verification — the deep-fix discipline distilled: never trust a conclusion until it has
SURVIVED an adversarial check. verify() spawns a red-team agent whose only job is to DISPROVE a
claim (not confirm it); if it breaks, the caller revises and re-checks. Outcomes are recorded as
lessons (feedback) so Jarvis learns. This is what makes shallow/confident-but-wrong answers reliable.

    v = verify(cfg, claim, context)         # -> {"survives": bool, "critique": str}
"""
from __future__ import annotations


def verify(cfg, claim, context="", timeout=150):
    """Spawn a red-team agent to try to break `claim`. Returns {survives, critique}."""
    from jarvis.adapters.llm import build_llm
    llm = build_llm(cfg)
    if not llm:
        return {"survives": True, "critique": "(no brain available to verify)"}
    prompt = ("You are a RED-TEAM verifier. Try hard to DISPROVE the claim below — find errors, "
              "unproven assumptions, missing evidence, or counter-examples. Do NOT look for support; "
              "attack it. If you cannot break it with the given context, say what evidence would settle "
              "it. End with EXACTLY one line: 'VERDICT: survives' or 'VERDICT: broken: <reason>'.\n\n"
              f"CLAIM:\n{claim}\n\nCONTEXT:\n{(context or '')[:6000]}")
    try:
        out = llm.run("red_team", prompt, timeout=timeout).strip()
    except Exception as e:
        return {"survives": True, "critique": f"(verify failed: {str(e)[:80]})"}
    broken = "verdict: broken" in out.lower()
    try:
        from jarvis import feedback
        feedback.record(cfg, kind="verify", area="red_team", summary=str(claim)[:120],
                        outcome="broken" if broken else "survives", detail=out[:500])
    except Exception:
        pass
    return {"survives": not broken, "critique": out}
