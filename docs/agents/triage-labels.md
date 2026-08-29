# Triage labels

These labels map the official engineering-skill roles to this repository:

| Role | Label | Meaning |
| --- | --- | --- |
| needs-triage | needs-triage | Maintainer must evaluate the issue |
| needs-info | needs-info | Waiting for more information |
| ready-for-agent | ready-for-agent | Spec is complete and the issue is eligible for an AFK engine |
| ready-for-human | ready-for-human | Requires human implementation |
| wontfix | wontfix | Will not be actioned |

AFK execution labels are separate state or authorization markers:

| Label | Meaning |
| --- | --- |
| agent:implement | Explicit execution authorization for the matching workflow |
| agent:queued | Ready, but a native blocker remains |
| agent:in-progress | An AFK run is active |
| agent:blocked | The run failed or the issue shape is invalid |
