## MODIFIED Requirements

### Requirement: Idle models release their accelerator memory

Murmly SHALL release the accelerator memory held by the transcription model, and
the memory held by the synthesis session, after each has been unused for its own
configured idle period. Releasing MUST return the memory to the system rather than
to an internal pool, so that another process can allocate it.

Accelerator memory SHALL be returned on every platform, because the runtime holding
it frees it when the model is dropped. System memory is the platform's allocator to
return, and not every allocator can be asked to. Where the platform provides no way
to ask, Murmly MUST report that it cannot rather than claiming it did, because the
person set an idle period to get memory back and is entitled to know the setting
did not do that here. Murmly MUST still drop the model on schedule in that case:
the memory becomes available for reuse within the process even where it is not
handed back to the system, and the next release on a platform that can ask depends
on nothing being held.

Each is governed independently: the transcription model and the synthesis session
have separate idle periods and are released separately. The memory each reclaims
and the time each costs to restore differ by roughly a factor of four in opposite
directions, so one shared period cannot serve both.

Murmly MUST NOT release a model that is in use. A release MUST NOT interrupt a
transcription pass, a synthesis in progress, or playback.

#### Scenario: Transcription model released after its idle period

- **GIVEN** the transcription model is resident and its idle period is configured
- **WHEN** no capture has been active for longer than that period
- **THEN** Murmly releases the accelerator memory the model held
- **AND** the memory is observable as free to other processes

#### Scenario: Synthesis session released on its own period

- **GIVEN** both models are resident and each has a different idle period
- **WHEN** only the synthesis period has elapsed
- **THEN** Murmly releases the synthesis session
- **AND** the transcription model remains resident

#### Scenario: A pass in progress is never interrupted

- **GIVEN** an idle period has elapsed
- **WHEN** a transcription pass or a synthesis is still running
- **THEN** Murmly does not release that model until the work completes

#### Scenario: A platform whose allocator cannot be asked to return memory

- **GIVEN** a platform that offers no way to ask the allocator to return freed
  system memory
- **WHEN** an idle period elapses and the model is released
- **THEN** Murmly drops the model on schedule
- **AND** the accelerator memory it held is returned
- **AND** diagnostics report that system memory is not returned to the system here
- **AND** Murmly does not report that it returned memory it did not
