# QA Scenario Ground-Truth Review

## Review status

- Schema: `qa-scenarios/v1`
- Proposed set: six injected faults and three fault-free controls
- Primary reviewer: _pending_
- Review date: _pending_
- Decision: **Pending**

Task 9 must not begin until a human reviewer verifies every row below and records an approval in the sign-off section. The fault identifier is evaluator-private: it may appear in `manifest.json`, but it must not appear in agent observations, planning requests, `steps.jsonl`, or final-assessment inputs.

## Proposed ground truth

| Scenario | Class | Difficulty | Expected behavior | Reproduction procedure |
|---|---|---|---|---|
| `easy-health-ratio` | Fault: `health_ratio_out_of_range` | Easy | Reported health, maximum health, and health ratio are internally consistent in one observation. | Enter gameplay and inspect one player-state observation. |
| `easy-relative-position` | Fault: `relative_position_mismatch` | Easy | Each entity's relative position equals its world position minus the player's world position. | Enter gameplay and compare one visible entity with the player in one observation. |
| `medium-upgrade-effect` | Fault: `upgrade_ack_without_effect` | Medium | An acknowledged upgrade selection changes the selected ability's owned state or level. | Reach an upgrade dialog, select an option, and compare ability inventory before and after acknowledgement. |
| `medium-chest-transition` | Fault: `chest_collected_without_state_transition` | Medium | A chest collection event corresponds to a changed chest count or run-progress state. | Enter gameplay, collect a tracked chest, and compare chest and progress state around the event. |
| `hard-experience-drift` | Fault: `experience_level_drift` | Hard | Experience, next-level requirement, and reported ratio remain consistent after multiple level transitions. | Enter gameplay, gain at least three levels, and inspect accumulated experience state. |
| `hard-restart-currency` | Fault: `currency_leak_across_restart` | Hard | Run-scoped coins reset to zero after gameplay restarts. | Accumulate gameplay progress, restart the level, and compare run currency before and after the scene transition. |
| `control-valid-observation` | Control | Easy | A fault-free observation satisfies health and relative-position invariants. | Enter gameplay and inspect one current-state observation. |
| `control-normal-transitions` | Control | Medium | Fault-free chest and upgrade acknowledgements produce their corresponding state changes. | Collect a chest, select an upgrade, and inspect both resulting transitions. |
| `control-long-progression` | Control | Hard | Fault-free long progression preserves experience consistency and restart currency isolation. | Sustain gameplay through multiple progression events, restart if available, and inspect cumulative state. |

## Difficulty rationale

- Easy scenarios require a contradiction within one observation.
- Medium scenarios require comparing an action or event with the immediately resulting state.
- Hard scenarios require evidence accumulated across multiple progression or scene transitions.

## Reviewer checklist

- [ ] The expected behavior for each scenario matches intended game behavior.
- [ ] Each reproduction procedure can expose the declared behavior on macOS.
- [ ] Each difficulty label reflects temporal reasoning length, not implementation effort.
- [ ] Each fault changes only its declared observation or transition signal.
- [ ] The three controls cover the same observation horizons without injected faults.
- [ ] Fault identifiers remain isolated from the agent channel.

## Primary reviewer sign-off

Reviewer name or identifier: _pending_

Date: _pending_

Decision: _pending (`approved` or `changes requested`)_

Notes: _pending_
