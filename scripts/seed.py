#!/usr/bin/env python3
"""Seed a fresh clone with a user, preferences, a profile, and a real knowledge graph.

    python -m scripts.seed

A clone of this repository is not useful until there is something in it. The resume engine
is a *view* over ``KnowledgeFact`` rows (golden rule #6), so an empty database cannot
generate a resume, cannot score a posting against anything, and makes every screen in the
desktop app look broken. This script is the difference between "it installed" and "it works".

**Everything here is idempotent.** Each row is looked up by its natural key before it is
written — the user by email, sources by ``(user_id, kind, uri)``, documents by
``(source_id, uri)``, entities by ``(user_id, kind, normalized_name)``, facts by
``content_hash``, edges by ``(source, target, relation)``. Running it ten times produces
exactly the same database as running it once, which is what lets it sit inside ``make dev``
and inside a CI job without either one having to know whether it already ran.

**Nothing here is fabricated on the user's behalf.** The seed persona is obviously a persona
— ``ada.embedded@example.invalid``, on the RFC 2606 reserved TLD that can never resolve — and
every fact traces to a seeded ``KnowledgeDocument`` exactly as a real indexed fact would. The
point is to exercise the same code paths real data takes, not to pre-fill a real résumé.

The domain flavour is deliberate: embedded firmware, robotics, C++ and CUDA. That is the
worked example ``docs/CONTRACTS.md`` §10 scores against, so a freshly seeded install produces
a *meaningful* score against a real posting instead of a uniform zero.

Options::

    python -m scripts.seed                      # seed, or top up an existing seed
    python -m scripts.seed --email me@host      # a different account
    python -m scripts.seed --reset              # delete the seed user, then re-seed
    python -m scripts.seed --quiet              # exit code only

It requires the schema to already exist (``alembic upgrade head`` / ``make migrate``) and
says so, with the command to run, rather than creating tables behind Alembic's back — a
schema created by ``create_all`` has no ``alembic_version`` row, and the next real migration
then fails on tables it thinks it has to create.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

import structlog

if TYPE_CHECKING:  # pragma: no cover - imports are deferred; see _seed()
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.knowledge import KnowledgeDocument, KnowledgeEntity, KnowledgeSource
    from app.models.user import User

__all__ = [
    "SEED_EMAIL",
    "SEED_FACTS",
    "SEED_FULL_NAME",
    "SeedReport",
    "main",
    "seed",
]

logger = structlog.get_logger(__name__)


# ======================================================================================
# Constants
# ======================================================================================

#: The seeded account. ``.invalid`` is reserved by RFC 2606 and can never resolve, so this
#: address cannot collide with a real one and cannot accidentally receive mail.
SEED_EMAIL: Final[str] = "ada.embedded@example.invalid"

#: Display name, and the name printed on any resume generated from this seed.
SEED_FULL_NAME: Final[str] = "Ada Okonkwo"

#: Rough characters-per-token used for ``KnowledgeDocument.token_count``. The budget
#: accounting only needs an order of magnitude, and a real tokenizer here would pull a model
#: dependency into a script that must run with zero API keys.
CHARS_PER_TOKEN: Final[int] = 4

#: Exit code returned when the schema has not been migrated yet.
EXIT_NO_SCHEMA: Final[int] = 2

#: Exit code for an unexpected failure.
EXIT_FAILURE: Final[int] = 1

#: Confidence stamped on every seeded fact. Below 1.0 on purpose: these are seeded, not
#: user-verified, and ``user_verified`` stays ``False`` so a real indexing pass may refine
#: them.
SEED_CONFIDENCE: Final[float] = 0.9


# ======================================================================================
# The seed data
# ======================================================================================


@dataclass(frozen=True, slots=True)
class SeedSource:
    """One knowledge source the persona pointed the indexer at.

    Attributes:
        key: Internal handle used by :class:`SeedDocument` to name its parent.
        kind: ``SourceKind`` member value.
        uri: URL, path, or provider identifier — half of the source's natural key.
        label: Human name shown in the desktop app.
    """

    key: str
    kind: str
    uri: str
    label: str


@dataclass(frozen=True, slots=True)
class SeedDocument:
    """One extracted artifact belonging to a :class:`SeedSource`.

    Attributes:
        key: Internal handle used by :class:`SeedFact` to name its provenance.
        source: :attr:`SeedSource.key` of the owning source.
        kind: ``SourceKind`` member value for this specific artifact.
        uri: Location within the source — the other half of the document's natural key.
        title: Human-facing name.
        raw_text: The extracted text a real analyzer would have produced.
    """

    key: str
    source: str
    kind: str
    uri: str
    title: str
    raw_text: str


@dataclass(frozen=True, slots=True)
class SeedEntity:
    """One node in the persona's knowledge graph.

    Attributes:
        kind: ``EntityKind`` member value.
        name: Display name, in the casing a source would have written it.
        summary: Short description shown in the graph view.
        mention_count: Prominence signal used when ranking resume bullets.
    """

    kind: str
    name: str
    summary: str
    mention_count: int


@dataclass(frozen=True, slots=True)
class SeedEdge:
    """One typed relationship between two seeded entities.

    Direction follows ``docs/OPEN_QUESTIONS.md`` (web analyzers, item 1): ``used_in`` reads
    subject-first and runs *technology → project*, matching what every analyzer emits. A
    seed that used the intuitive reverse would create a second, parallel edge for every pair
    the first real indexing pass touches.

    Attributes:
        source: ``"<kind>:<name>"`` of the subject entity.
        relation: ``RelationKind`` member value.
        target: ``"<kind>:<name>"`` of the object entity.
        weight: Strength, used to rank graph expansion during retrieval.
    """

    source: str
    relation: str
    target: str
    weight: float = 1.0


@dataclass(frozen=True, slots=True)
class SeedFact:
    """One atomic, source-attributed claim — the unit a resume bullet is generated from.

    Attributes:
        kind: ``FactKind`` member value.
        text: The claim as a human should read it.
        organization: Employer, school or project it belongs to.
        role: Title held while it was true.
        date_start: Period start, as a source would have written it. Never parsed.
        date_end: Period end, as written. ``"Present"`` is legitimate.
        skills: Skills evidenced by the claim.
        technologies: Technologies used.
        metrics: Quantified outcomes, quoted verbatim.
        impact_score: 0-100 prominence, used when a resume must shrink to one page.
        document: :attr:`SeedDocument.key` this claim was "extracted" from.
        entity: ``"<kind>:<name>"`` of the primary graph node, if any.
    """

    kind: str
    text: str
    organization: str | None
    role: str | None
    date_start: str | None
    date_end: str | None
    impact_score: int
    document: str
    entity: str | None = None
    skills: tuple[str, ...] = field(default_factory=tuple)
    technologies: tuple[str, ...] = field(default_factory=tuple)
    metrics: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class SeedMemory:
    """One remembered preference or correction.

    Attributes:
        kind: ``MemoryKind`` member value.
        text: The remembered content, in natural language.
        weight: Multiplier applied to relevance during retrieval.
    """

    kind: str
    text: str
    weight: float = 1.0


# --------------------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------------------

SEED_SOURCES: Final[tuple[SeedSource, ...]] = (
    SeedSource(
        key="github",
        kind="github_profile",
        uri="https://github.com/ada-embedded",
        label="GitHub — ada-embedded",
    ),
    SeedSource(
        key="projects",
        kind="project_folder",
        uri="~/projects",
        label="Local project folder",
    ),
    SeedSource(
        key="resume",
        kind="resume",
        uri="documents/ada-okonkwo-resume-2026.pdf",
        label="Existing resume (2026)",
    ),
)


# --------------------------------------------------------------------------------------
# Documents
# --------------------------------------------------------------------------------------

SEED_DOCUMENTS: Final[tuple[SeedDocument, ...]] = (
    SeedDocument(
        key="flight_controller",
        source="github",
        kind="readme",
        uri="https://github.com/ada-embedded/flight-controller#readme",
        title="flight-controller — README",
        raw_text=(
            "# flight-controller\n\n"
            "A FreeRTOS-based flight control stack for an STM32F405 quadrotor. The attitude "
            "loop runs at 1kHz off the SPI IMU DMA transfer; the position loop runs at 100Hz "
            "off a fused GPS/barometer estimate.\n\n"
            "## Design notes\n"
            "Task latency was the whole problem. The original superloop missed its 1kHz "
            "deadline under logging load, so the scheduler was reworked to a rate-monotonic "
            "assignment with priority inheritance on the two shared SPI mutexes, which cut "
            "worst-case attitude-loop jitter from 340us to 18us.\n\n"
            "The IMU driver reads over DMA into a double buffer, so the control task never "
            "blocks on the bus. Flash budget mattered on a 1MB part: link-time optimisation "
            "and a custom printf shim brought the image from 412KB to 231KB.\n\n"
            "## Testing\n"
            "Hardware-in-the-loop: the flight dynamics model runs on the host, the firmware "
            "runs on the real board, and the two exchange state over a 2Mbaud UART. 94% of "
            "the control code is covered by the HIL suite.\n"
        ),
    ),
    SeedDocument(
        key="cuda_raytracer",
        source="github",
        kind="readme",
        uri="https://github.com/ada-embedded/cuda-pathtracer#readme",
        title="cuda-pathtracer — README",
        raw_text=(
            "# cuda-pathtracer\n\n"
            "A physically-based path tracer in CUDA C++17, written to learn where GPU "
            "occupancy actually goes.\n\n"
            "## Performance work\n"
            "The naive megakernel spent most of its time diverged. Splitting it into "
            "wavefront stages with a persistent-threads scheduler took the Cornell box from "
            "42ms to 9ms per frame at 1080p on an RTX 3070 — a 4.6x speedup.\n\n"
            "The BVH is built with a linear-BVH radix sort on device; rebuilding a 2.1M "
            "triangle scene takes 11ms. Shared-memory traversal stacks removed 71% of the "
            "global memory traffic that Nsight Compute attributed to the traversal loop.\n\n"
            "Denoising is an A-trous wavelet filter guided by the albedo and normal buffers, "
            "which lets the tracer converge acceptably at 4 samples per pixel.\n"
        ),
    ),
    SeedDocument(
        key="slam",
        source="github",
        kind="readme",
        uri="https://github.com/ada-embedded/vio-slam#readme",
        title="vio-slam — README",
        raw_text=(
            "# vio-slam\n\n"
            "Visual-inertial odometry for a ground robot, ROS 2 Humble, C++17.\n\n"
            "Front end is FAST corners tracked with pyramidal Lucas-Kanade in OpenCV; back "
            "end is a sliding-window bundle adjustment over 12 keyframes with IMU "
            "preintegration between them. Absolute trajectory error on EuRoC MH_03 is 0.081m "
            "RMSE.\n\n"
            "It runs at 28 FPS on a Jetson Orin Nano with the feature front end moved onto "
            "the GPU with a hand-written CUDA kernel; the CPU-only front end managed 11 FPS.\n"
        ),
    ),
    SeedDocument(
        key="internship_notes",
        source="projects",
        kind="project_folder",
        uri="~/projects/vector-robotics/notes.md",
        title="Vector Robotics — internship notes",
        raw_text=(
            "# Vector Robotics — firmware internship\n\n"
            "Owned the motor-controller firmware for the AMR drive base: Zephyr RTOS on a "
            "dual-core STM32H7, FOC commutation at 20kHz, CAN-FD to the vehicle computer.\n\n"
            "Shipped: a bootloader with A/B slots and CRC rollback, which took field firmware "
            "updates from a 3% brick rate to zero across 240 units. Wrote the CI hardware "
            "farm harness — six boards on a rack, every merge flashed and smoke-tested in "
            "under four minutes.\n\n"
            "Found and fixed a priority-inversion deadlock in the CAN driver that had been "
            "showing up as a once-a-week watchdog reset in the field.\n"
        ),
    ),
    SeedDocument(
        key="team_notes",
        source="projects",
        kind="project_folder",
        uri="~/projects/robotics-team/retrospective.md",
        title="University Robotics Team — retrospective",
        raw_text=(
            "# Robotics team — embedded software lead\n\n"
            "Led six people on the embedded stack for the competition rover. Set up the "
            "monorepo, the CMake cross-toolchain and the hardware-in-the-loop rig everyone "
            "else built against.\n\n"
            "We placed 3rd of 41 at the national competition. The autonomy stack — ROS 2 on "
            "the compute module, custom firmware on four STM32 nodes over CAN — completed the "
            "full course, which two of the three teams above us did not.\n\n"
            "Mentored four first-years through their first embedded project; three of them "
            "took firmware internships the following summer.\n"
        ),
    ),
    SeedDocument(
        key="resume_pdf",
        source="resume",
        kind="resume",
        uri="documents/ada-okonkwo-resume-2026.pdf",
        title="Ada Okonkwo — resume (2026)",
        raw_text=(
            "ADA OKONKWO\n"
            "Embedded Systems and GPU Computing\n\n"
            "EDUCATION\n"
            "BS Computer Engineering, minor in Mathematics. GPA 3.87. Graduating May 2026.\n"
            "Coursework: Real-Time Systems, Computer Architecture, Parallel Computing, "
            "Robotics, Digital Signal Processing.\n\n"
            "EXPERIENCE\n"
            "Firmware Engineering Intern, Vector Robotics (Summer 2025)\n"
            "Embedded Software Lead, University Robotics Team (2024-2025)\n"
            "Undergraduate Researcher, GPU Systems Lab (2024)\n\n"
            "SKILLS\n"
            "C, C++17, Python, Rust, CUDA, FreeRTOS, Zephyr, ROS 2, STM32, CAN-FD, OpenCV, "
            "TensorRT, CMake, Git, Docker, oscilloscope and logic-analyser bring-up.\n"
        ),
    ),
)


# --------------------------------------------------------------------------------------
# Graph
# --------------------------------------------------------------------------------------

SEED_ENTITIES: Final[tuple[SeedEntity, ...]] = (
    # -- technologies ------------------------------------------------------------------
    SeedEntity("technology", "FreeRTOS", "Real-time kernel used on the STM32 flight stack.", 9),
    SeedEntity("technology", "Zephyr", "RTOS used for the motor-controller firmware.", 5),
    SeedEntity("technology", "STM32", "Cortex-M microcontroller family used across projects.", 12),
    SeedEntity("technology", "CUDA", "GPU compute platform used for rendering and vision.", 11),
    SeedEntity("technology", "ROS 2", "Robotics middleware for the rover and the VIO stack.", 7),
    SeedEntity("technology", "OpenCV", "Computer-vision library used in the SLAM front end.", 4),
    SeedEntity("technology", "TensorRT", "Inference runtime used on the Jetson deployment.", 2),
    SeedEntity("technology", "CAN-FD", "Vehicle bus between the drive base and the computer.", 5),
    SeedEntity("technology", "CMake", "Cross-compilation build for the embedded monorepo.", 4),
    # -- skills ------------------------------------------------------------------------
    SeedEntity("skill", "C", "Systems language used for all firmware work.", 10),
    SeedEntity("skill", "C++", "Primary language for GPU and robotics work (C++17).", 13),
    SeedEntity("skill", "Python", "Tooling, test harnesses and data analysis.", 8),
    SeedEntity("skill", "Rust", "Used for host-side tooling and a CAN bus decoder.", 3),
    SeedEntity("skill", "Real-time scheduling", "Rate-monotonic analysis and inheritance.", 6),
    SeedEntity("skill", "GPU performance engineering", "Occupancy, divergence, memory traffic.", 6),
    # -- projects ----------------------------------------------------------------------
    SeedEntity("project", "Flight controller", "FreeRTOS quadrotor flight control stack.", 8),
    SeedEntity("project", "CUDA path tracer", "Wavefront path tracer in CUDA C++17.", 7),
    SeedEntity("project", "Visual-inertial SLAM", "ROS 2 visual-inertial odometry for a rover.", 6),
    # -- organizations and roles -------------------------------------------------------
    SeedEntity("organization", "Vector Robotics", "Autonomous mobile robot company.", 6),
    SeedEntity("organization", "University Robotics Team", "Student competition robotics team.", 5),
    SeedEntity("organization", "GPU Systems Lab", "Undergraduate research lab.", 3),
    SeedEntity("role", "Firmware Engineering Intern", "Summer 2025 internship role.", 3),
    SeedEntity("role", "Embedded Software Lead", "Team leadership role, 2024-2025.", 3),
)

SEED_EDGES: Final[tuple[SeedEdge, ...]] = (
    # technology -> project (subject-first, matching every analyzer in app/knowledge/)
    SeedEdge("technology:FreeRTOS", "used_in", "project:Flight controller", 1.0),
    SeedEdge("technology:STM32", "used_in", "project:Flight controller", 1.0),
    SeedEdge("technology:CUDA", "used_in", "project:CUDA path tracer", 1.0),
    SeedEdge("technology:CUDA", "used_in", "project:Visual-inertial SLAM", 0.6),
    SeedEdge("technology:ROS 2", "used_in", "project:Visual-inertial SLAM", 1.0),
    SeedEdge("technology:OpenCV", "used_in", "project:Visual-inertial SLAM", 0.8),
    # skill -> project
    SeedEdge("skill:C", "used_in", "project:Flight controller", 1.0),
    SeedEdge("skill:C++", "used_in", "project:CUDA path tracer", 1.0),
    SeedEdge("skill:C++", "used_in", "project:Visual-inertial SLAM", 1.0),
    SeedEdge("skill:Real-time scheduling", "used_in", "project:Flight controller", 1.0),
    SeedEdge("skill:GPU performance engineering", "used_in", "project:CUDA path tracer", 1.0),
    # employment and authorship
    SeedEdge("role:Firmware Engineering Intern", "worked_at", "organization:Vector Robotics", 1.0),
    SeedEdge(
        "role:Embedded Software Lead",
        "worked_at",
        "organization:University Robotics Team",
        1.0,
    ),
    SeedEdge("technology:Zephyr", "used_in", "organization:Vector Robotics", 0.9),
    SeedEdge("technology:CAN-FD", "used_in", "organization:Vector Robotics", 0.9),
    SeedEdge("technology:CMake", "used_in", "organization:University Robotics Team", 0.7),
)


# --------------------------------------------------------------------------------------
# Facts — the rows a resume is actually generated from
# --------------------------------------------------------------------------------------

SEED_FACTS: Final[tuple[SeedFact, ...]] = (
    # -- Vector Robotics ---------------------------------------------------------------
    SeedFact(
        kind="accomplishment",
        text=(
            "Shipped an A/B slot bootloader with CRC rollback for a Zephyr-based motor "
            "controller, taking the field firmware-update brick rate from 3% to zero across "
            "240 deployed units"
        ),
        organization="Vector Robotics",
        role="Firmware Engineering Intern",
        date_start="2025-05",
        date_end="2025-08",
        impact_score=94,
        document="internship_notes",
        entity="organization:Vector Robotics",
        skills=("C", "Embedded systems", "Bootloaders"),
        technologies=("Zephyr", "STM32", "CRC"),
        metrics=("3% to zero", "240 units"),
    ),
    SeedFact(
        kind="accomplishment",
        text=(
            "Diagnosed and fixed a priority-inversion deadlock in a CAN-FD driver that had "
            "been surfacing as a weekly watchdog reset across the deployed fleet"
        ),
        organization="Vector Robotics",
        role="Firmware Engineering Intern",
        date_start="2025-05",
        date_end="2025-08",
        impact_score=86,
        document="internship_notes",
        entity="technology:CAN-FD",
        skills=("Debugging", "Real-time scheduling", "C"),
        technologies=("CAN-FD", "Zephyr"),
    ),
    SeedFact(
        kind="accomplishment",
        text=(
            "Built a six-board hardware-in-the-loop CI farm that flashes and smoke-tests "
            "every merge in under four minutes"
        ),
        organization="Vector Robotics",
        role="Firmware Engineering Intern",
        date_start="2025-05",
        date_end="2025-08",
        impact_score=81,
        document="internship_notes",
        entity="organization:Vector Robotics",
        skills=("Python", "Test automation", "CI/CD"),
        technologies=("STM32", "Docker"),
        metrics=("six boards", "under four minutes"),
    ),
    SeedFact(
        kind="responsibility",
        text=(
            "Owned the motor-controller firmware for an autonomous mobile robot drive base: "
            "field-oriented commutation at 20kHz on a dual-core STM32H7, CAN-FD to the "
            "vehicle computer"
        ),
        organization="Vector Robotics",
        role="Firmware Engineering Intern",
        date_start="2025-05",
        date_end="2025-08",
        impact_score=78,
        document="internship_notes",
        entity="technology:STM32",
        skills=("C", "Motor control", "Embedded systems"),
        technologies=("Zephyr", "STM32", "CAN-FD"),
        metrics=("20kHz",),
    ),
    # -- University Robotics Team ------------------------------------------------------
    SeedFact(
        kind="leadership_item",
        text=(
            "Led a six-person embedded software team through a competition rover build, "
            "placing 3rd of 41 nationally with the only autonomy stack in the top four to "
            "complete the full course"
        ),
        organization="University Robotics Team",
        role="Embedded Software Lead",
        date_start="2024-09",
        date_end="2025-05",
        impact_score=92,
        document="team_notes",
        entity="organization:University Robotics Team",
        skills=("Leadership", "Systems architecture", "ROS 2"),
        technologies=("ROS 2", "STM32", "CAN-FD"),
        metrics=("3rd of 41",),
    ),
    SeedFact(
        kind="accomplishment",
        text=(
            "Reworked a missed-deadline superloop into a rate-monotonic FreeRTOS schedule "
            "with priority inheritance on the shared SPI mutexes, cutting worst-case "
            "attitude-loop jitter from 340us to 18us"
        ),
        organization="University Robotics Team",
        role="Embedded Software Lead",
        date_start="2024-09",
        date_end="2025-05",
        impact_score=95,
        document="flight_controller",
        entity="skill:Real-time scheduling",
        skills=("Real-time scheduling", "C", "Embedded systems"),
        technologies=("FreeRTOS", "STM32"),
        metrics=("340us to 18us", "1kHz"),
    ),
    SeedFact(
        kind="accomplishment",
        text=(
            "Cut a 1MB-flash firmware image from 412KB to 231KB with link-time optimisation "
            "and a custom printf shim, leaving room for the logging subsystem"
        ),
        organization="University Robotics Team",
        role="Embedded Software Lead",
        date_start="2024-09",
        date_end="2025-05",
        impact_score=74,
        document="flight_controller",
        entity="technology:STM32",
        skills=("C", "Toolchains", "Embedded systems"),
        technologies=("CMake", "STM32", "GCC"),
        metrics=("412KB to 231KB",),
    ),
    SeedFact(
        kind="accomplishment",
        text=(
            "Built a hardware-in-the-loop rig that runs the flight dynamics model on the host "
            "and the real firmware on the board over a 2Mbaud UART, reaching 94% coverage of "
            "the control code"
        ),
        organization="University Robotics Team",
        role="Embedded Software Lead",
        date_start="2024-09",
        date_end="2025-05",
        impact_score=83,
        document="flight_controller",
        entity="project:Flight controller",
        skills=("Python", "Test automation", "Embedded systems"),
        technologies=("FreeRTOS", "STM32"),
        metrics=("94% coverage", "2Mbaud"),
    ),
    SeedFact(
        kind="leadership_item",
        text=(
            "Mentored four first-year engineers through their first embedded project; three "
            "went on to firmware internships the following summer"
        ),
        organization="University Robotics Team",
        role="Embedded Software Lead",
        date_start="2024-09",
        date_end="2025-05",
        impact_score=68,
        document="team_notes",
        entity="role:Embedded Software Lead",
        skills=("Mentoring", "Leadership"),
        metrics=("four engineers", "three internships"),
    ),
    # -- GPU Systems Lab and personal GPU work -----------------------------------------
    SeedFact(
        kind="accomplishment",
        text=(
            "Restructured a diverged CUDA megakernel into wavefront stages driven by a "
            "persistent-threads scheduler, taking a 1080p path-traced frame from 42ms to 9ms "
            "on an RTX 3070"
        ),
        organization="GPU Systems Lab",
        role="Undergraduate Researcher",
        date_start="2024-01",
        date_end="2024-08",
        impact_score=96,
        document="cuda_raytracer",
        entity="project:CUDA path tracer",
        skills=("CUDA", "C++", "GPU performance engineering"),
        technologies=("CUDA", "Nsight Compute"),
        metrics=("42ms to 9ms", "4.6x"),
    ),
    SeedFact(
        kind="accomplishment",
        text=(
            "Moved BVH traversal stacks into shared memory, removing 71% of the global memory "
            "traffic Nsight Compute attributed to the traversal loop"
        ),
        organization="GPU Systems Lab",
        role="Undergraduate Researcher",
        date_start="2024-01",
        date_end="2024-08",
        impact_score=88,
        document="cuda_raytracer",
        entity="skill:GPU performance engineering",
        skills=("CUDA", "GPU performance engineering", "C++"),
        technologies=("CUDA",),
        metrics=("71%",),
    ),
    SeedFact(
        kind="accomplishment",
        text=(
            "Implemented a device-side linear-BVH radix sort that rebuilds a 2.1M-triangle "
            "acceleration structure in 11ms, making fully dynamic scenes practical"
        ),
        organization="GPU Systems Lab",
        role="Undergraduate Researcher",
        date_start="2024-01",
        date_end="2024-08",
        impact_score=79,
        document="cuda_raytracer",
        entity="technology:CUDA",
        skills=("CUDA", "Algorithms", "C++"),
        technologies=("CUDA",),
        metrics=("2.1M triangles", "11ms"),
    ),
    SeedFact(
        kind="skill_usage",
        text=(
            "Wrote a hand-optimised CUDA feature-tracking kernel that took a ROS 2 "
            "visual-inertial odometry stack from 11 FPS to 28 FPS on a Jetson Orin Nano"
        ),
        organization="GPU Systems Lab",
        role="Undergraduate Researcher",
        date_start="2024-01",
        date_end="2024-08",
        impact_score=90,
        document="slam",
        entity="project:Visual-inertial SLAM",
        skills=("CUDA", "Computer vision", "C++"),
        technologies=("CUDA", "ROS 2", "OpenCV", "TensorRT"),
        metrics=("11 FPS to 28 FPS",),
    ),
    SeedFact(
        kind="metric",
        text=(
            "Achieved 0.081m RMSE absolute trajectory error on the EuRoC MH_03 sequence with "
            "a sliding-window bundle adjustment over 12 keyframes and IMU preintegration"
        ),
        organization="GPU Systems Lab",
        role="Undergraduate Researcher",
        date_start="2024-01",
        date_end="2024-08",
        impact_score=85,
        document="slam",
        entity="project:Visual-inertial SLAM",
        skills=("Computer vision", "Sensor fusion", "C++"),
        technologies=("ROS 2", "OpenCV"),
        metrics=("0.081m RMSE",),
    ),
    # -- Education and skills ----------------------------------------------------------
    SeedFact(
        kind="education_item",
        text=(
            "BS Computer Engineering with a Mathematics minor, GPA 3.87, coursework in "
            "Real-Time Systems, Computer Architecture, Parallel Computing and Robotics"
        ),
        organization="State Polytechnic University",
        role=None,
        date_start="2022-08",
        date_end="2026-05",
        impact_score=70,
        document="resume_pdf",
        skills=("Computer architecture", "Real-time systems", "Parallel computing"),
        metrics=("3.87 GPA",),
    ),
    SeedFact(
        kind="skill_usage",
        text=(
            "Daily bring-up work with an oscilloscope and a logic analyser on SPI, I2C and "
            "CAN buses, including protocol decode of intermittent hardware faults"
        ),
        organization=None,
        role=None,
        date_start="2023-01",
        date_end="Present",
        impact_score=62,
        document="resume_pdf",
        skills=("Hardware bring-up", "Debugging"),
        technologies=("SPI", "I2C", "CAN-FD"),
    ),
    SeedFact(
        kind="skill_usage",
        text=(
            "Wrote a host-side CAN bus decoder in Rust to replace a Python tool that could "
            "not keep up with a 5Mbit/s bus"
        ),
        organization=None,
        role=None,
        date_start="2025-02",
        date_end="2025-04",
        impact_score=64,
        document="resume_pdf",
        entity="skill:Rust",
        skills=("Rust", "Systems programming"),
        technologies=("CAN-FD",),
        metrics=("5Mbit/s",),
    ),
    SeedFact(
        kind="award",
        text=("Placed 3rd of 41 teams at the national collegiate robotics competition, 2025"),
        organization="University Robotics Team",
        role="Embedded Software Lead",
        date_start="2025-04",
        date_end="2025-04",
        impact_score=76,
        document="team_notes",
        entity="organization:University Robotics Team",
        metrics=("3rd of 41",),
    ),
)


# --------------------------------------------------------------------------------------
# Memory
# --------------------------------------------------------------------------------------

SEED_MEMORIES: Final[tuple[SeedMemory, ...]] = (
    SeedMemory(
        kind="preference",
        text=(
            "Prefers roles where firmware or GPU work is the product rather than a support "
            "function; deprioritise general web and business-application roles."
        ),
        weight=1.5,
    ),
    SeedMemory(
        kind="correction",
        text=(
            "Write the language as 'C++17', never 'CPP' or 'Cplusplus'. ATS keyword matching "
            "on the literal string is the reason it matters."
        ),
        weight=1.2,
    ),
    SeedMemory(
        kind="note",
        text=(
            "Graduating May 2026, so new-grad and internship postings with a 2026 start are "
            "in scope and senior postings are not."
        ),
        weight=1.0,
    ),
)


# --------------------------------------------------------------------------------------
# Profile and preferences
# --------------------------------------------------------------------------------------

#: Structured answers to what an application form asks. Every value here is a *fact* about
#: the persona, not policy — policy lives in :data:`SEED_PREFERENCES`.
SEED_PROFILE: Final[dict[str, Any]] = {
    "phone": "+1-555-0142",
    "pronouns": "she/her",
    "location": "Austin, TX, USA",
    "address": {
        "line1": "1200 Congress Ave",
        "city": "Austin",
        "region": "TX",
        "postal_code": "78701",
        "country": "USA",
    },
    "links": {
        "github": "https://github.com/ada-embedded",
        "linkedin": "https://www.linkedin.com/in/ada-okonkwo-embedded",
        "portfolio": "https://ada-embedded.example.invalid",
        "website": None,
        "other": {},
    },
    "citizenship": "United States",
    "work_authorization": "citizen",
    "requires_sponsorship": False,
    # EEO fields are seeded to the explicit decline sentinel, never to an inferred value.
    # ``None`` would mean "not yet asked", which makes every form field unanswerable and
    # routes every application to manual review (golden rule #2) — the seed would look
    # broken. The sentinel means "asked, declined", which is submittable.
    "gender": "decline_to_self_identify",
    "race_ethnicity": "decline_to_self_identify",
    "disability_status": "decline_to_self_identify",
    "veteran_status": "decline_to_self_identify",
    "clearance": "none",
    "salary_min": 115_000,
    "salary_max": 165_000,
    "salary_currency": "USD",
    "remote_preference": "hybrid",
    "willing_to_relocate": True,
    "relocation_targets": ["Austin, TX", "Seattle, WA", "Boston, MA", "San Diego, CA"],
    "desired_roles": [
        "Embedded Software Engineer",
        "Firmware Engineer",
        "Robotics Software Engineer",
        "GPU Software Engineer",
        "Systems Software Engineer",
    ],
    "desired_industries": ["Robotics", "Aerospace", "Semiconductors", "Autonomous vehicles"],
    "excluded_companies": [],
    "excluded_industries": ["Gambling", "Adtech"],
    "education": [
        {
            "institution": "State Polytechnic University",
            "degree": "BS",
            "field_of_study": "Computer Engineering",
            "start": "2022-08",
            "end": "2026-05",
            "gpa": "3.87",
            "honors": "Dean's List (6 semesters)",
        }
    ],
    "start_date_availability": "June 2026",
    "notice_period_weeks": 2,
    "extra": {},
}

#: The policy the automation obeys. ``auto_apply`` stays ``False``: the seed sets up the
#: knowledge, never the permission. Golden rule #3 requires a deliberate flip, and a seed
#: script is not a deliberate flip.
SEED_PREFERENCES: Final[dict[str, Any]] = {
    "min_score": 72,
    "auto_apply": False,
    "max_applications_per_day": 25,
    "max_essay_questions": 2,
    "min_salary": 115_000,
    "preferred_locations": ["Austin, TX", "Remote", "Seattle, WA"],
    "preferred_keywords": [
        "embedded",
        "firmware",
        "RTOS",
        "robotics",
        "CUDA",
        "C++",
        "real-time",
        "new grad",
    ],
    "blocked_companies": [],
    "blocked_industries": ["Gambling", "Adtech"],
    "exclude_defense": False,
    "remote_only": False,
    "require_no_sponsorship": True,
    "resume_template": "modern",
    "cover_letter_policy": "when_required",
    "providers_enabled": ["greenhouse", "lever", "ashby"],
}


# ======================================================================================
# Report
# ======================================================================================


@dataclass(slots=True)
class SeedReport:
    """What the run actually wrote, split by created versus already present.

    Attributes:
        user_email: The account that was seeded.
        user_created: Whether the user row itself was new.
        created: Row counts inserted this run, by table label.
        existing: Row counts already present, by table label.
        embedded: Number of facts and entities that received an embedding vector.
        embedding_note: Why embedding was or was not performed.
    """

    user_email: str
    user_created: bool = False
    created: dict[str, int] = field(default_factory=dict)
    existing: dict[str, int] = field(default_factory=dict)
    embedded: int = 0
    embedding_note: str = ""

    def record(self, label: str, *, was_created: bool) -> None:
        """Count one row against *label*.

        Args:
            label: Table label, e.g. ``"facts"``.
            was_created: Whether this run inserted it.
        """
        bucket = self.created if was_created else self.existing
        bucket[label] = bucket.get(label, 0) + 1

    @property
    def total_created(self) -> int:
        """Total rows inserted by this run."""
        return sum(self.created.values())

    def render(self) -> str:
        """Return a human-readable summary block.

        Returns:
            A multi-line string listing every label with its created/existing split.
        """
        labels = sorted(set(self.created) | set(self.existing))
        width = max((len(label) for label in labels), default=0)
        lines = [
            f"user            {self.user_email}"
            f"{'  (created)' if self.user_created else '  (already present)'}",
            "",
        ]
        lines.extend(
            f"  {label.ljust(width)}   +{self.created.get(label, 0):<4} "
            f"({self.existing.get(label, 0)} already present)"
            for label in labels
        )
        lines.append("")
        lines.append(f"  embeddings     {self.embedded} written - {self.embedding_note}")
        return "\n".join(lines)


# ======================================================================================
# Helpers
# ======================================================================================


def _digest(text: str) -> str:
    """Return the SHA-256 hex digest of *text*.

    ``hashlib``, never :func:`hash`: the built-in is salted per process, so a stored value
    derived from it would not match itself after a restart.

    Args:
        text: The text to digest.

    Returns:
        A 64-character lowercase hex digest.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _entity_key(kind: str, name: str) -> str:
    """Return the ``"<kind>:<name>"`` handle used to reference an entity in the seed data.

    Args:
        kind: ``EntityKind`` member value.
        name: Entity display name.

    Returns:
        The composite handle.
    """
    return f"{kind}:{name}"


def _split_entity_key(key: str) -> tuple[str, str]:
    """Split a ``"<kind>:<name>"`` handle.

    Args:
        key: The handle, e.g. ``"technology:ROS 2"``.

    Returns:
        The ``(kind, name)`` pair. Only the first colon separates, so a name may contain one.

    Raises:
        ValueError: If *key* has no colon, which is a typo in the seed data rather than a
            runtime condition worth tolerating.
    """
    kind, separator, name = key.partition(":")
    if not separator:
        raise ValueError(f"malformed entity key {key!r}; expected '<kind>:<name>'")
    return kind, name


# ======================================================================================
# The seeding routine
# ======================================================================================


async def seed(email: str = SEED_EMAIL, *, reset: bool = False) -> SeedReport:
    """Create (or top up) the seed account and its knowledge graph.

    Args:
        email: Address of the account to seed.
        reset: Delete the account first, cascading through every owned row, then re-seed.
            Off by default — the normal call must be safe to repeat.

    Returns:
        A :class:`SeedReport` describing exactly what was written.

    Raises:
        RuntimeError: If the schema does not exist yet. Raised rather than papered over: a
            schema created here with ``create_all`` would carry no ``alembic_version`` row,
            and the next real migration would then fail on tables it believes it must create.
    """
    # Imports are deferred so that ``--help`` works on a machine with no database driver
    # installed: ``app.database.session`` builds the engine at import time from
    # ``settings.database_url``, which raises when the named driver is absent.
    from sqlalchemy import delete, select
    from sqlalchemy.exc import OperationalError, ProgrammingError

    from app.config.settings import get_settings
    from app.database.session import session_scope
    from app.models.user import User

    settings = get_settings()
    report = SeedReport(user_email=email)

    async with session_scope() as session:
        try:
            existing = await session.scalar(select(User).where(User.email == email.lower()))
        except (OperationalError, ProgrammingError) as exc:
            # ASCII only in anything printed to a terminal: this project targets Windows as
            # a first-class platform, and a console still on cp1252 renders an em dash as a
            # replacement character right in the middle of the instruction.
            raise RuntimeError(
                "the database has no schema yet - run `make migrate` "
                "(or `alembic upgrade head`) first"
            ) from exc

        if reset and existing is not None:
            logger.info("seed.reset", email=email, user_id=str(existing.id))
            await session.execute(delete(User).where(User.id == existing.id))
            await session.flush()
            existing = None

        user = existing
        if user is None:
            user = await _create_user(session, email)
            report.user_created = True

        await _ensure_profile(session, user, report)
        sources = await _ensure_sources(session, user, report)
        documents = await _ensure_documents(session, user, sources, report)
        entities = await _ensure_entities(session, user, report)
        await _ensure_edges(session, user, entities, report)
        facts = await _ensure_facts(session, user, documents, entities, report)
        await _ensure_memories(session, user, report)

        report.embedded, report.embedding_note = await _maybe_embed(
            settings, facts, list(entities.values())
        )

    logger.info(
        "seed.complete",
        email=email,
        created=report.total_created,
        embedded=report.embedded,
    )
    return report


async def _create_user(session: AsyncSession, email: str) -> User:
    """Insert the seed user with validated preferences.

    Args:
        session: The open session.
        email: Address for the new account.

    Returns:
        The flushed :class:`~app.models.user.User`, with its primary key populated.
    """
    from app.database.types import utcnow
    from app.models.user import User, UserPreferences

    user = User(email=email, full_name=SEED_FULL_NAME, is_active=True)
    # Through the typed bridge rather than assigning the raw JSON, so an invalid seed
    # preference fails here instead of silently degrading to defaults on first read.
    user.prefs = UserPreferences.model_validate(SEED_PREFERENCES)
    user.mark_onboarded(utcnow())
    session.add(user)
    await session.flush()
    logger.info("seed.user_created", email=email, user_id=str(user.id))
    return user


async def _ensure_profile(session: AsyncSession, user: User, report: SeedReport) -> None:
    """Create the user's profile row if it does not exist.

    An existing profile is left completely alone: it may hold answers a human typed into the
    onboarding wizard, and a seed script overwriting those would be data loss.

    Args:
        session: The open session.
        user: The owning user.
        report: Report to count the row against.
    """
    from sqlalchemy import select

    from app.models.profile import UserProfile

    found = await session.scalar(select(UserProfile).where(UserProfile.user_id == user.id))
    if found is not None:
        report.record("profile", was_created=False)
        return

    profile = UserProfile(user_id=user.id, **SEED_PROFILE)
    session.add(profile)
    await session.flush()
    report.record("profile", was_created=True)


async def _ensure_sources(
    session: AsyncSession, user: User, report: SeedReport
) -> dict[str, KnowledgeSource]:
    """Create the knowledge sources, keyed by their seed handle.

    Sources are marked ``indexed`` with a completion timestamp, because the documents and
    facts below are exactly what an indexing pass would have produced. Leaving them
    ``pending`` would show a permanently-unindexed source in the desktop app for data that
    is, in fact, fully indexed.

    Args:
        session: The open session.
        user: The owning user.
        report: Report to count rows against.

    Returns:
        ``{seed key: KnowledgeSource}``.
    """
    from sqlalchemy import select

    from app.database.types import utcnow
    from app.models.enums import IndexStatus, SourceKind
    from app.models.knowledge import KnowledgeSource

    resolved: dict[str, KnowledgeSource] = {}
    for spec in SEED_SOURCES:
        kind = SourceKind(spec.kind)
        found = await session.scalar(
            select(KnowledgeSource).where(
                KnowledgeSource.user_id == user.id,
                KnowledgeSource.kind == kind,
                KnowledgeSource.uri == spec.uri,
            )
        )
        if found is None:
            found = KnowledgeSource(
                user_id=user.id,
                kind=kind,
                uri=spec.uri,
                label=spec.label,
                enabled=True,
                auto_refresh=False,
                index_status=IndexStatus.INDEXED,
                last_indexed_at=utcnow(),
            )
            session.add(found)
            await session.flush()
            report.record("sources", was_created=True)
        else:
            report.record("sources", was_created=False)
        resolved[spec.key] = found
    return resolved


async def _ensure_documents(
    session: AsyncSession,
    user: User,
    sources: dict[str, KnowledgeSource],
    report: SeedReport,
) -> dict[str, KnowledgeDocument]:
    """Create the extracted documents, keyed by their seed handle.

    Args:
        session: The open session.
        user: The owning user.
        sources: Output of :func:`_ensure_sources`.
        report: Report to count rows against.

    Returns:
        ``{seed key: KnowledgeDocument}``.
    """
    from sqlalchemy import select

    from app.database.types import utcnow
    from app.models.enums import SourceKind
    from app.models.knowledge import KnowledgeDocument

    resolved: dict[str, KnowledgeDocument] = {}
    for spec in SEED_DOCUMENTS:
        parent = sources[spec.source]
        found = await session.scalar(
            select(KnowledgeDocument).where(
                KnowledgeDocument.source_id == parent.id,
                KnowledgeDocument.uri == spec.uri,
            )
        )
        if found is None:
            found = KnowledgeDocument(
                user_id=user.id,
                source_id=parent.id,
                kind=SourceKind(spec.kind),
                uri=spec.uri,
                title=spec.title,
                raw_text=spec.raw_text,
                content_hash=_digest(spec.raw_text),
                token_count=len(spec.raw_text) // CHARS_PER_TOKEN,
                metadata_json={"seeded": True},
                indexed_at=utcnow(),
            )
            session.add(found)
            await session.flush()
            report.record("documents", was_created=True)
        else:
            report.record("documents", was_created=False)
        resolved[spec.key] = found
    return resolved


async def _ensure_entities(
    session: AsyncSession, user: User, report: SeedReport
) -> dict[str, KnowledgeEntity]:
    """Create the graph nodes, keyed by ``"<kind>:<name>"``.

    Args:
        session: The open session.
        user: The owning user.
        report: Report to count rows against.

    Returns:
        ``{entity key: KnowledgeEntity}``.
    """
    from sqlalchemy import select

    from app.models.enums import EntityKind
    from app.models.knowledge import KnowledgeEntity

    resolved: dict[str, KnowledgeEntity] = {}
    for spec in SEED_ENTITIES:
        kind = EntityKind(spec.kind)
        normalized = KnowledgeEntity.normalize(spec.name)
        found = await session.scalar(
            select(KnowledgeEntity).where(
                KnowledgeEntity.user_id == user.id,
                KnowledgeEntity.kind == kind,
                KnowledgeEntity.normalized_name == normalized,
            )
        )
        if found is None:
            # ``normalized_name`` is derived by an @validates hook on ``name``; setting the
            # display name is enough and keeps the two from ever disagreeing.
            found = KnowledgeEntity(
                user_id=user.id,
                kind=kind,
                name=spec.name,
                summary=spec.summary,
                mention_count=spec.mention_count,
                confidence=SEED_CONFIDENCE,
            )
            session.add(found)
            await session.flush()
            report.record("entities", was_created=True)
        else:
            report.record("entities", was_created=False)
        resolved[_entity_key(spec.kind, spec.name)] = found
    return resolved


async def _ensure_edges(
    session: AsyncSession,
    user: User,
    entities: dict[str, KnowledgeEntity],
    report: SeedReport,
) -> None:
    """Create the graph edges between already-resolved entities.

    Args:
        session: The open session.
        user: The owning user.
        entities: Output of :func:`_ensure_entities`.
        report: Report to count rows against.

    Raises:
        KeyError: If an edge names an entity the seed data does not define. That is a typo
            in this file and should stop the run, not silently drop a relationship.
    """
    from sqlalchemy import select

    from app.models.enums import RelationKind
    from app.models.knowledge import KnowledgeEdge

    for spec in SEED_EDGES:
        source = entities[spec.source]
        target = entities[spec.target]
        relation = RelationKind(spec.relation)
        found = await session.scalar(
            select(KnowledgeEdge).where(
                KnowledgeEdge.source_entity_id == source.id,
                KnowledgeEdge.target_entity_id == target.id,
                KnowledgeEdge.relation == relation,
            )
        )
        if found is None:
            session.add(
                KnowledgeEdge(
                    user_id=user.id,
                    source_entity_id=source.id,
                    target_entity_id=target.id,
                    relation=relation,
                    weight=spec.weight,
                    evidence={"seeded": True},
                )
            )
            report.record("edges", was_created=True)
        else:
            report.record("edges", was_created=False)
    await session.flush()


async def _ensure_facts(
    session: AsyncSession,
    user: User,
    documents: dict[str, KnowledgeDocument],
    entities: dict[str, KnowledgeEntity],
    report: SeedReport,
) -> list[Any]:
    """Create the fact rows — the ones a generated resume is built from.

    Deduplication is by ``content_hash``, computed by the model itself from the normalised
    text plus organization, role and dates. Using the model's own arithmetic rather than a
    local reimplementation is what guarantees a seeded fact and an indexed fact with the
    same claim collapse onto one row instead of two.

    Args:
        session: The open session.
        user: The owning user.
        documents: Output of :func:`_ensure_documents`.
        entities: Output of :func:`_ensure_entities`.
        report: Report to count rows against.

    Returns:
        Every seeded fact row, created or pre-existing, for the embedding pass.
    """
    from sqlalchemy import select

    from app.models.enums import FactKind
    from app.models.knowledge import KnowledgeFact

    resolved: list[Any] = []
    for spec in SEED_FACTS:
        content_hash = KnowledgeFact.build_content_hash(
            normalized_text=KnowledgeFact.normalize_text(spec.text),
            organization=spec.organization,
            role=spec.role,
            date_start=spec.date_start,
            date_end=spec.date_end,
        )
        found = await session.scalar(
            select(KnowledgeFact).where(
                KnowledgeFact.user_id == user.id,
                KnowledgeFact.content_hash == content_hash,
            )
        )
        if found is None:
            entity = entities[spec.entity] if spec.entity else None
            found = KnowledgeFact(
                user_id=user.id,
                kind=FactKind(spec.kind),
                text=spec.text,
                organization=spec.organization,
                role=spec.role,
                date_start=spec.date_start,
                date_end=spec.date_end,
                skills=list(spec.skills),
                technologies=list(spec.technologies),
                metrics=list(spec.metrics),
                impact_score=spec.impact_score,
                confidence=SEED_CONFIDENCE,
                source_document_id=documents[spec.document].id,
                entity_id=entity.id if entity is not None else None,
                user_verified=False,
                is_active=True,
            )
            found.refresh_derived_fields()
            session.add(found)
            await session.flush()
            report.record("facts", was_created=True)
        else:
            report.record("facts", was_created=False)
        resolved.append(found)
    return resolved


async def _ensure_memories(session: AsyncSession, user: User, report: SeedReport) -> None:
    """Create the memory entries retrieved alongside facts.

    Memory rows have no natural key in the schema, so they are deduplicated here on
    ``(user_id, kind, text)`` — the same triple a repeated correction would produce.

    Args:
        session: The open session.
        user: The owning user.
        report: Report to count rows against.
    """
    from sqlalchemy import select

    from app.models.enums import MemoryKind
    from app.models.knowledge import MemoryEntry

    for spec in SEED_MEMORIES:
        kind = MemoryKind(spec.kind)
        found = await session.scalar(
            select(MemoryEntry).where(
                MemoryEntry.user_id == user.id,
                MemoryEntry.kind == kind,
                MemoryEntry.text == spec.text,
            )
        )
        if found is None:
            session.add(
                MemoryEntry(
                    user_id=user.id,
                    kind=kind,
                    text=spec.text,
                    weight=spec.weight,
                    context={"seeded": True},
                )
            )
            report.record("memories", was_created=True)
        else:
            report.record("memories", was_created=False)
    await session.flush()


async def _maybe_embed(settings: Any, facts: list[Any], entities: list[Any]) -> tuple[int, str]:
    """Embed the seeded rows, but only when doing so costs nothing.

    Retrieval fuses a keyword ranking with a vector ranking, so facts without embeddings are
    already retrievable and a resume can be generated without this step. Embeddings simply
    make the semantic half work from the first run.

    The gate is deliberate: this runs only when the resolved provider is the offline hashing
    embedder. A seed script must never spend the user's OpenAI budget on eighteen sentences
    they did not ask to have embedded — a real indexing pass will do it with their consent.

    Args:
        settings: The process settings.
        facts: Fact rows to embed.
        entities: Entity rows to embed.

    Returns:
        ``(rows embedded, reason)``. The reason is reported either way, so a zero is never
        mistaken for a failure.
    """
    from app.ai.embeddings import HASHING_PROVIDER, embed_texts, resolve_embedding_provider

    provider = resolve_embedding_provider(settings)
    if provider != HASHING_PROVIDER:
        return 0, (
            f"skipped: EMBEDDING_PROVIDER resolved to {provider!r}, which bills per call. "
            "Facts stay keyword-retrievable; index a source to embed them."
        )

    pending = [row for row in [*facts, *entities] if row.embedding is None]
    if not pending:
        return 0, "nothing to embed (every seeded row already has a vector)"

    texts = [row.text if hasattr(row, "text") else row.name for row in pending]
    vectors = await embed_texts(texts)
    for row, vector in zip(pending, vectors, strict=True):
        row.embedding = vector
    return len(pending), f"offline hashing embedder, {settings.embedding_dim} dimensions"


# ======================================================================================
# Entry point
# ======================================================================================


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, run the seed, and print what happened.

    Args:
        argv: Command-line arguments, or ``None`` to read :data:`sys.argv`.

    Returns:
        ``0`` on success, :data:`EXIT_NO_SCHEMA` when the schema is missing, and
        :data:`EXIT_FAILURE` on any other failure.
    """
    parser = argparse.ArgumentParser(
        prog="python -m scripts.seed",
        description=__doc__.splitlines()[0] if __doc__ else "Seed the database.",
    )
    parser.add_argument(
        "--email",
        default=SEED_EMAIL,
        help=f"account to seed (default: {SEED_EMAIL})",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="delete the account and everything it owns, then re-seed",
    )
    parser.add_argument("--quiet", action="store_true", help="print nothing; exit code only")
    args = parser.parse_args(argv)

    try:
        report = asyncio.run(seed(args.email, reset=args.reset))
    except RuntimeError as exc:
        print(f"seed: {exc}", file=sys.stderr)
        return EXIT_NO_SCHEMA
    except Exception as exc:
        print(f"seed: failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_FAILURE

    if not args.quiet:
        print()
        print(report.render())
        print()
        if report.total_created:
            print("  A tailored resume can be generated from this graph now.")
        else:
            print("  Already seeded - nothing to do. Use --reset to rebuild it.")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
