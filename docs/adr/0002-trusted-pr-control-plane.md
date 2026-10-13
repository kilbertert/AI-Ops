# Trusted control plane for pull request mutation

Status: Accepted

AFK pull request mutation workflows accept only repository-owner-authored branches from the same repository. Host orchestration and dependencies come from the default-branch controller, candidate commands receive only the read token inside Docker, and resulting commits cross into a clean delivery checkout through a verified Git bundle before the host injects `AGENT_PAT` for the final push; missing or failed delivery credentials stop the workflow. This preserves downstream CI triggers without executing candidate-controlled host code with delivery credentials.

## Alternatives considered

- **One reusable workflow or composite action shared by the review, implement, and
  update-branch workflows.** The option this record is mostly about, and the
  original text carries both it and why it lost: those three keep their
  control-plane steps local because their triggers, preflight state, and write
  phases differ, so a shared unit is only correct once tests can prove that the
  owner/fork gates, candidate isolation, token scope, bundle verification, and
  failure-label behaviour stay unchanged **for every caller**. Before that proof
  exists, adopting it moves the divergence from three readable files into one
  file whose callers disagree silently.

<!-- This record is a pre-convention one (written 2026-08-30; this convention
     arrived 2026-10-11). It recorded the alternative above and no others, so the
     section names one option rather than filling it out — inventing the options
     nobody wrote down is the failure the convention names, not the fix for it.
     The `## Alternatives considered` heading and this note were added later, when
     the shape check began asking; the argument itself is unchanged. -->
