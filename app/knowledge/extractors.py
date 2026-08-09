"""Turning raw text into structured knowledge — the deterministic core of the engine.

Every analyzer eventually holds a wall of prose: a README, a portfolio page, a resume, a
LinkedIn position description. This module is what converts that into the
:class:`~app.knowledge.analyzers.base.ExtractedFact`,
:class:`~app.knowledge.analyzers.base.ExtractedEntity` and
:class:`~app.knowledge.analyzers.base.ExtractedEdge` rows that a resume is later generated
*from* (golden rules #6 and #7).

**There are two paths, and both must work.**

*Rule-based* (:func:`extract_facts_rule_based`, :func:`extract_entities_rule_based`) is
pure Python: no network, no API key, no model. It recovers bullets, matches a curated
technology vocabulary, pulls quantified outcomes and date ranges out of the text, and
scores each claim for impact with an explainable heuristic. This is the path that runs on a
laptop with nothing installed, and it is the fallback for every failure of the other path.

*LLM-assisted* (:class:`KnowledgeExtractor`) asks a model to do the same job with better
judgement — and then **checks its homework**. A returned claim whose wording does not
substantially appear in the source is dropped as a hallucination; metrics that are not in
the source are dropped; organizations, roles and dates the model invented are dropped;
skills are canonicalised through :data:`SKILL_VOCABULARY` and unrecognised prose is
discarded. Any failure at all — no API key, a timeout, a malformed response, a budget
refusal, an empty result — falls back to the rule-based path and logs
``extractor.fallback_rule_based``. :meth:`KnowledgeExtractor.extract` never raises.

**Matching is word-boundary aware and never matches inside a word.** ``"go"`` does not
match inside ``"google"``, ``"C"`` does not match inside ``"CI"`` or inside ``"C++"``, and
``"ROS"`` does not match inside ``"ROS2"``. Aliases are tried longest-first so ``"C++"``
wins over ``"C"``, and ``"Node.js"`` over ``"Node"``, at the same position.

Deduplication is shared with the ORM: :func:`normalize_fact_text` and
:func:`fact_content_hash` delegate to :class:`~app.models.knowledge.KnowledgeFact`, so a
fact hashed here and the row it becomes agree by construction rather than by coincidence.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Any, Final

import structlog

from app.models.enums import EntityKind, FactKind, RelationKind
from app.models.knowledge import KnowledgeFact, normalize_for_matching

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from app.ai.llm import ModelPlugin
    from app.cache.base import Cache
    from app.knowledge.analyzers.base import (
        AnalysisResult,
        ExtractedEdge,
        ExtractedEntity,
        ExtractedFact,
    )

__all__ = [
    "ACTION_VERBS",
    "CONCEPT_SKILLS",
    "FILLER_PHRASES",
    "LEADERSHIP_TERMS",
    "MAX_IMPACT",
    "MIN_IMPACT",
    "MIN_SOURCE_OVERLAP",
    "SCALE_TERMS",
    "SKILL_VOCABULARY",
    "KnowledgeExtractor",
    "canonical_skill",
    "classify_skills",
    "detect_project_name",
    "extract_dates",
    "extract_entities_rule_based",
    "extract_facts_rule_based",
    "extract_metrics",
    "extract_skills",
    "fact_content_hash",
    "normalize_fact_text",
    "score_impact",
    "skill_entity_kind",
    "split_bullets",
]

logger = structlog.get_logger(__name__)


# ======================================================================================
# The skill vocabulary
# ======================================================================================

#: Canonical technology/skill name → the other surface forms that mean the same thing.
#:
#: The canonical name is always matchable in its own right; aliases only add spellings.
#: Alias matching is case-insensitive, tolerant of ``-``/``_``/space differences ("bare
#: metal" == "bare-metal" == "baremetal"), and strictly word-bounded, so no entry can ever
#: fire inside a longer word.
#:
#: Curation rules, learned the hard way:
#:
#: * **No ambiguous two-letter aliases.** ``"cv"`` (curriculum vitae), ``"tf"`` (transfer
#:   function), ``"np"``, ``"ts"`` and a bare ``"node"`` — which in this user's robotics
#:   domain overwhelmingly means a *ROS node* — are all deliberately absent.
#: * **No aliases that are ordinary English.** ``"rest"``, ``"make"``, ``"express"`` and
#:   ``"lambda"`` would each fire on prose several times per document.
#: * **Version-suffixed spellings are listed explicitly** (``c++17``, ``yolov8``,
#:   ``ue5``): the right-hand word boundary correctly refuses to match ``C++`` inside
#:   ``C++17``, so the longer form has to be its own alias to be seen at all.
SKILL_VOCABULARY: Final[dict[str, tuple[str, ...]]] = {
    # -- languages -------------------------------------------------------------------
    "C": ("c language", "ansi c", "c99", "c11", "c17"),
    "C++": ("cpp", "c plus plus", "cxx", "c++11", "c++14", "c++17", "c++20", "c++23"),
    "C#": ("csharp", "c sharp"),
    "Python": ("python3", "python 3", "cpython"),
    "Rust": ("rustlang", "rust lang"),
    "Go": ("golang", "go lang"),
    "Java": ("java se", "java ee"),
    "TypeScript": ("type script",),
    "JavaScript": ("js", "ecmascript", "es6", "es2015"),
    "Lua": ("lua 5.1", "lua5.1"),
    "Luau": ("roblox lua",),
    "MATLAB": ("matlab script",),
    "Simulink": ("simscape",),
    "VHDL": ("vhdl rtl",),
    "Verilog": ("verilog hdl",),
    "SystemVerilog": ("system verilog", "uvm"),
    "Assembly": ("asm", "x86 assembly", "arm assembly", "inline assembly"),
    "Bash": ("shell scripting", "shell script", "bash scripting"),
    "SQL": ("t-sql", "plpgsql", "pl/sql"),
    "Kotlin": (),
    "Swift": ("swiftui",),
    "Ruby": ("ruby on rails", "rails"),
    "PHP": (),
    "Haskell": (),
    "Perl": (),
    # -- embedded, electronics and robotics ------------------------------------------
    "RTOS": ("real-time operating system", "real time os"),
    "FreeRTOS": ("free rtos",),
    "Zephyr": ("zephyr rtos", "zephyr os"),
    "ROS": ("robot operating system",),
    "ROS 2": ("ros2", "ros 2 humble", "ros2 humble", "rclcpp", "rclpy"),
    "CAN bus": ("canbus", "controller area network", "can-fd", "canopen"),
    "I2C": ("i²c", "iic", "i2c bus"),
    "SPI": ("spi bus",),
    "UART": ("usart", "serial uart"),
    "PCB design": ("pcb", "printed circuit board", "pcb layout", "schematic capture"),
    "KiCad": ("kicad eda",),
    "Altium": ("altium designer",),
    "STM32": ("stm32f4", "stm32f1", "stm32h7", "stm32 hal", "stmcube", "stm32cubeide"),
    "ESP32": ("esp-idf", "esp idf", "esp32-s3"),
    "ESP8266": ("nodemcu",),
    "Arduino": ("arduino ide", "avr", "atmega"),
    "Raspberry Pi": ("raspberrypi", "rpi", "raspberry pi 4", "raspberry pi 5"),
    "Nordic nRF": ("nrf52", "nrf53", "nrf connect"),
    "Firmware": ("firmware development", "embedded firmware", "firmware engineering"),
    "Bare-metal programming": ("bare metal", "baremetal", "no-os", "register-level"),
    "Device drivers": ("device driver", "driver development", "kernel driver", "hal driver"),
    "PID control": ("pid controller", "pid tuning", "pid loop", "pid"),
    "SLAM": ("simultaneous localization and mapping", "visual slam", "orb-slam"),
    "Kinematics": ("inverse kinematics", "forward kinematics", "ik solver", "jacobian"),
    "Sensor fusion": ("sensor-fusion", "multi-sensor fusion"),
    "Kalman filter": ("extended kalman filter", "ekf", "unscented kalman filter", "ukf"),
    "Embedded systems": ("embedded system", "embedded software", "embedded development"),
    "Embedded Linux": ("yocto", "buildroot", "petalinux"),
    "Motor control": ("bldc", "brushless motor", "servo control", "stepper motor", "foc"),
    "Robotics": ("robotic", "robotics engineering", "mobile robotics"),
    "Gazebo": ("gazebo sim", "ignition gazebo"),
    "MoveIt": ("moveit2", "moveit 2"),
    "Signal processing": ("dsp", "digital signal processing", "fft", "fir filter"),
    "Control theory": ("state-space control", "lqr", "model predictive control", "mpc"),
    "JTAG": ("swd", "openocd", "st-link", "segger j-link"),
    "Oscilloscope": ("logic analyzer", "oscilloscopes", "bench testing"),
    "Modbus": ("modbus rtu", "modbus tcp"),
    "MQTT": ("mqtt broker",),
    "BLE": ("bluetooth low energy", "bluetooth"),
    "LoRa": ("lorawan",),
    "Zigbee": (),
    "IMU": ("inertial measurement unit", "accelerometer", "gyroscope", "magnetometer"),
    "LiDAR": ("lidar sensor", "velodyne", "rplidar"),
    "FPGA": ("field programmable gate array", "xilinx", "vivado", "quartus"),
    "3D printing": ("fdm printing", "additive manufacturing", "prusa"),
    "CAD": ("solidworks", "fusion 360", "onshape", "autocad"),
    "Soldering": ("smd soldering", "reflow soldering", "rework"),
    # -- machine learning and computer vision -----------------------------------------
    "PyTorch": ("torch", "py torch", "pytorch lightning", "libtorch"),
    "TensorFlow": ("tensor flow", "tensorflow lite", "tflite"),
    "Keras": (),
    "CUDA": ("cuda c", "nvidia cuda", "cudnn"),
    "OpenCV": ("cv2", "open cv"),
    "YOLO": ("yolov3", "yolov5", "yolov7", "yolov8", "you only look once"),
    "TensorRT": ("tensor rt",),
    "scikit-learn": ("sklearn", "scikit learn"),
    "NumPy": ("numerical python",),
    "pandas": ("pandas dataframe",),
    "SciPy": (),
    "Matplotlib": ("pyplot",),
    "ONNX": ("onnx runtime", "onnxruntime"),
    "Jetson": ("nvidia jetson", "jetson nano", "jetson orin", "jetson xavier"),
    "Hugging Face": ("huggingface", "transformers library"),
    "Computer vision": ("computer-vision", "image processing", "object detection"),
    "Machine learning": ("machine-learning", "ml"),
    "Deep learning": ("deep neural network", "dnn", "neural network"),
    "Reinforcement learning": ("deep reinforcement learning", "q-learning", "ppo"),
    "Point cloud processing": ("pcl", "point cloud library", "open3d"),
    "Data analysis": ("data analytics", "exploratory data analysis"),
    # -- web, backend and cloud --------------------------------------------------------
    "React": ("react.js", "reactjs", "react hooks"),
    "Next.js": ("nextjs", "next js"),
    "Vue": ("vue.js", "vuejs"),
    "Svelte": ("sveltekit",),
    "Electron": ("electron.js", "electronjs"),
    "FastAPI": ("fast api",),
    "Django": ("django rest framework", "drf"),
    "Flask": (),
    "Node.js": ("nodejs", "node.js"),
    "Express.js": ("expressjs", "express js"),
    "Docker": ("dockerfile", "docker compose", "containerization"),
    "Kubernetes": ("k8s", "kubectl", "helm"),
    "AWS": ("amazon web services", "ec2", "s3 bucket", "dynamodb", "cloudfront"),
    "GCP": ("google cloud", "google cloud platform", "bigquery"),
    "Azure": ("microsoft azure",),
    "PostgreSQL": ("postgres", "psql", "pgvector"),
    "Redis": ("redis cache",),
    "MongoDB": ("mongo", "mongoose"),
    "SQLite": ("sqlite3",),
    "GraphQL": ("apollo graphql",),
    "REST API": ("rest apis", "restful api", "restful", "rest endpoint"),
    "WebSocket": ("websockets", "web socket", "socket.io"),
    "gRPC": ("protobuf", "protocol buffers"),
    "Tailwind CSS": ("tailwind", "tailwindcss"),
    "Celery": ("celery worker",),
    "RabbitMQ": ("amqp",),
    "Kafka": ("apache kafka",),
    "Terraform": ("hashicorp terraform",),
    "nginx": ("reverse proxy",),
    "Supabase": (),
    "Firebase": ("firestore",),
    # -- tooling and practice -----------------------------------------------------------
    "Git": ("git rebase", "git workflow"),
    "GitHub": ("github pull request",),
    "Linux": ("ubuntu", "debian", "gnu/linux", "arch linux"),
    "CMake": ("cmakelists", "cmake build"),
    "Bazel": (),
    "Makefile": ("gnu make", "make build system"),
    "PlatformIO": ("platformio ini",),
    "pytest": ("py.test",),
    "GoogleTest": ("gtest", "google test"),
    "CI/CD": (
        "continuous integration",
        "continuous deployment",
        "continuous delivery",
        "github actions",
        "gitlab ci",
        "jenkins",
        "ci pipeline",
    ),
    "Unit testing": ("unit tests", "test-driven development", "tdd", "integration testing"),
    "GDB": ("gdb debugger", "lldb"),
    "Valgrind": ("address sanitizer", "asan"),
    "Wireshark": ("packet capture", "tcpdump"),
    "Agile": ("scrum", "kanban", "agile development", "sprint planning"),
    "Technical writing": ("documentation writing", "api documentation"),
    "Code review": ("peer review", "pull request review"),
    "PowerShell": ("power shell",),
    "Vim": ("neovim",),
    "LaTeX": ("latex typesetting", "tectonic"),
    # -- game development ----------------------------------------------------------------
    "Roblox": ("roblox studio", "rojo"),
    "Unity": ("unity3d", "unity engine"),
    "Unreal Engine": ("unreal", "ue4", "ue5"),
    "Godot": ("gdscript",),
    "Blender": (),
    "Shader programming": ("glsl", "hlsl", "shaders", "compute shader"),
    "Game design": ("game development", "gamedev", "level design"),
    "Physics simulation": ("rigid body simulation", "collision detection", "bullet physics"),
}

#: Canonical names that are a *discipline* rather than a named technology. They populate
#: ``ExtractedFact.skills`` and become :attr:`~app.models.enums.EntityKind.SKILL` nodes;
#: everything else in :data:`SKILL_VOCABULARY` populates ``ExtractedFact.technologies`` and
#: becomes an :attr:`~app.models.enums.EntityKind.TECHNOLOGY` node. Keeping the two lists
#: disjoint is what stops "PyTorch" appearing twice in the same fact.
CONCEPT_SKILLS: Final[frozenset[str]] = frozenset(
    {
        "Agile",
        "Bare-metal programming",
        "CAD",
        "CI/CD",
        "Code review",
        "Computer vision",
        "Control theory",
        "Data analysis",
        "Deep learning",
        "Device drivers",
        "Embedded systems",
        "Firmware",
        "Game design",
        "Kalman filter",
        "Kinematics",
        "Machine learning",
        "Motor control",
        "PCB design",
        "PID control",
        "Physics simulation",
        "Point cloud processing",
        "REST API",
        "Reinforcement learning",
        "Robotics",
        "SLAM",
        "Sensor fusion",
        "Shader programming",
        "Signal processing",
        "Soldering",
        "Technical writing",
        "Unit testing",
        "3D printing",
    }
)

#: Verbs that mark a claim as *doing something* rather than *being present somewhere*.
#: Both the base and past forms are listed, because matching is exact-token: inferring
#: "optimize" from "optimized" would need a stemmer, and a stemmer would also turn
#: "designed" into "design" in "design document", which is not a claim at all.
ACTION_VERBS: Final[frozenset[str]] = frozenset(
    {
        "accelerated",
        "accelerate",
        "achieved",
        "achieve",
        "architected",
        "architect",
        "authored",
        "author",
        "automated",
        "automate",
        "benchmarked",
        "benchmark",
        "boosted",
        "boost",
        "built",
        "build",
        "calibrated",
        "calibrate",
        "characterized",
        "collaborated",
        "containerized",
        "converted",
        "coordinated",
        "coordinate",
        "created",
        "create",
        "cut",
        "debugged",
        "debug",
        "delivered",
        "deliver",
        "deployed",
        "deploy",
        "designed",
        "design",
        "developed",
        "develop",
        "diagnosed",
        "doubled",
        "drove",
        "drive",
        "eliminated",
        "eliminate",
        "engineered",
        "engineer",
        "enhanced",
        "established",
        "expanded",
        "extended",
        "fabricated",
        "fixed",
        "founded",
        "generated",
        "halved",
        "hardened",
        "implemented",
        "implement",
        "improved",
        "improve",
        "increased",
        "increase",
        "instrumented",
        "integrated",
        "integrate",
        "introduced",
        "launched",
        "launch",
        "led",
        "lead",
        "mentored",
        "mentor",
        "migrated",
        "migrate",
        "minimized",
        "modeled",
        "modernized",
        "monitored",
        "negotiated",
        "optimized",
        "optimize",
        "orchestrated",
        "organized",
        "overhauled",
        "parallelized",
        "pioneered",
        "ported",
        "port",
        "prototyped",
        "prototype",
        "presented",
        "profiled",
        "published",
        "quantized",
        "ranked",
        "rearchitected",
        "rebuilt",
        "redesigned",
        "reduced",
        "reduce",
        "refactored",
        "refactor",
        "rewrote",
        "resolved",
        "restructured",
        "saved",
        "scaled",
        "scale",
        "secured",
        "shipped",
        "ship",
        "simulated",
        "simplified",
        "solved",
        "spearheaded",
        "standardized",
        "streamlined",
        "strengthened",
        "supervised",
        "taught",
        "tested",
        "trained",
        "tripled",
        "tuned",
        "tune",
        "validated",
        "vectorized",
        "verified",
        "won",
        "wrote",
        "write",
    }
)

#: Phrases that describe proximity to work rather than ownership of it. Each one costs the
#: bullet real points: "responsible for the CAN driver" tells a recruiter nothing that
#: "rewrote the CAN driver, cutting jitter 91%" does not tell them better.
FILLER_PHRASES: Final[tuple[str, ...]] = (
    "responsible for",
    "helped with",
    "helped to",
    "worked on",
    "working on",
    "assisted with",
    "assisted in",
    "involved in",
    "participated in",
    "familiar with",
    "exposure to",
    "duties included",
    "tasked with",
    "part of a team",
    "learned about",
    "gained experience",
    "various tasks",
    "in charge of",
)

#: Words that signal ownership or direction of other people's work.
LEADERSHIP_TERMS: Final[tuple[str, ...]] = (
    "led",
    "lead",
    "leading",
    "owned",
    "ownership",
    "founded",
    "co-founded",
    "mentored",
    "mentoring",
    "managed",
    "managing",
    "directed",
    "spearheaded",
    "captain",
    "president",
    "chair",
    "head of",
    "principal",
    "supervised",
    "coordinated",
    "taught",
    "trained",
    "organized",
    "team lead",
    "tech lead",
)

#: Words that signal the work happened at real scale rather than in a sandbox.
SCALE_TERMS: Final[tuple[str, ...]] = (
    "production",
    "enterprise",
    "nationwide",
    "company-wide",
    "org-wide",
    "large-scale",
    "high-throughput",
    "high-availability",
    "real-time",
    "24/7",
    "fleet",
    "cross-functional",
    "team of",
    "customers",
    "end users",
    "deployed to",
    "flight",
    "safety-critical",
    "mission-critical",
)


# ======================================================================================
# Vocabulary matching
# ======================================================================================

#: Left boundary. ``+`` and ``#`` are included so ``C`` cannot match the ``C`` of ``C++``
#: from the right-hand side, and digits so ``I2C`` cannot match inside ``SPI2C``.
_BOUNDARY_LEFT: Final[str] = r"(?<![0-9A-Za-z_+#])"

#: Right boundary. This single expression is what makes ``"go"`` refuse ``"google"``,
#: ``"C"`` refuse ``"CI"`` and ``"C++"``, and ``"ROS"`` refuse ``"ROS2"``.
_BOUNDARY_RIGHT: Final[str] = r"(?![0-9A-Za-z_+#])"

#: Separator inside a multi-word alias. Zero-or-more, so one entry covers "bare metal",
#: "bare-metal", "bare_metal" and "baremetal".
_ALIAS_SEPARATOR: Final[str] = r"[\s\-_]*"

#: Splits an alias into its words for pattern construction and key normalisation.
_ALIAS_WORD_SPLIT: Final[re.Pattern[str]] = re.compile(r"[\s\-_]+")


def _alias_pattern(alias: str) -> str:
    """Build the word-bounded, separator-tolerant regex source for one alias.

    Args:
        alias: The surface form to match, e.g. ``"bare metal"`` or ``"C++"``.

    Returns:
        A regex fragment that matches *alias* only as a whole token sequence.
    """
    words = [word for word in _ALIAS_WORD_SPLIT.split(alias.strip()) if word]
    body = _ALIAS_SEPARATOR.join(re.escape(word) for word in words)
    return f"{_BOUNDARY_LEFT}{body}{_BOUNDARY_RIGHT}"


def _alias_keys(alias: str) -> tuple[str, str]:
    """Return the two lookup keys an alias can be recovered under.

    Because :data:`_ALIAS_SEPARATOR` permits zero separator, matched text may arrive either
    spaced ("can bus") or compacted ("canbus"); both map back to the same canonical name.

    Args:
        alias: The surface form.

    Returns:
        ``(spaced_key, compact_key)``, both case-folded.
    """
    words = [word for word in _ALIAS_WORD_SPLIT.split(alias.strip().casefold()) if word]
    return (" ".join(words), "".join(words))


def _build_alias_index() -> tuple[dict[str, str], dict[str, str], re.Pattern[str]]:
    """Compile :data:`SKILL_VOCABULARY` into two lookup maps and one master pattern.

    Aliases are ordered longest-first so that at any given position the most specific
    surface form wins: ``C++17`` before ``C++`` before ``C``, ``ROS 2`` before ``ROS``,
    ``Node.js`` before ``Node``. ``re`` alternation is leftmost-first, and ``finditer``
    does not produce overlapping matches, so a single scan is enough.

    Returns:
        ``(spaced_index, compact_index, master_pattern)``.
    """
    spaced: dict[str, str] = {}
    compact: dict[str, str] = {}
    surfaces: set[str] = set()

    for canonical, aliases in SKILL_VOCABULARY.items():
        for surface in (canonical, *aliases):
            cleaned = surface.strip()
            if not cleaned:
                continue
            surfaces.add(cleaned)
            spaced_key, compact_key = _alias_keys(cleaned)
            spaced.setdefault(spaced_key, canonical)
            compact.setdefault(compact_key, canonical)

    ordered = sorted(surfaces, key=lambda surface: (-len(surface), surface.casefold()))
    pattern = re.compile(
        "|".join(_alias_pattern(surface) for surface in ordered),
        re.IGNORECASE,
    )
    return spaced, compact, pattern


_ALIAS_BY_SPACED: Final[dict[str, str]]
_ALIAS_BY_COMPACT: Final[dict[str, str]]
_VOCABULARY_PATTERN: Final[re.Pattern[str]]
_ALIAS_BY_SPACED, _ALIAS_BY_COMPACT, _VOCABULARY_PATTERN = _build_alias_index()


def canonical_skill(value: str) -> str | None:
    """Resolve one surface form to its canonical vocabulary name.

    Args:
        value: A skill or technology name from any source — a model's JSON, a GitHub
            language field, a manifest dependency.

    Returns:
        The canonical name, or ``None`` when *value* is not in the vocabulary.
    """
    if not value:
        return None
    spaced_key, compact_key = _alias_keys(value)
    return _ALIAS_BY_SPACED.get(spaced_key) or _ALIAS_BY_COMPACT.get(compact_key)


def extract_skills(text: str) -> list[str]:
    """Return every vocabulary skill and technology *text* mentions.

    One scan with the master pattern, so no name is counted twice and the longest matching
    alias always wins at a given position.

    Args:
        text: Free-form text.

    Returns:
        Canonical names, deduplicated, in order of first appearance.
    """
    if not text:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for match in _VOCABULARY_PATTERN.finditer(text):
        canonical = canonical_skill(match.group(0))
        if canonical is None or canonical in seen:
            continue
        seen.add(canonical)
        found.append(canonical)
    return found


def skill_entity_kind(canonical: str) -> EntityKind:
    """Return the graph node type for a canonical vocabulary name.

    Args:
        canonical: A canonical name from :data:`SKILL_VOCABULARY`.

    Returns:
        :attr:`~app.models.enums.EntityKind.SKILL` for a discipline (see
        :data:`CONCEPT_SKILLS`), otherwise
        :attr:`~app.models.enums.EntityKind.TECHNOLOGY`.
    """
    return EntityKind.SKILL if canonical in CONCEPT_SKILLS else EntityKind.TECHNOLOGY


def classify_skills(names: Iterable[str]) -> tuple[list[str], list[str]]:
    """Partition canonical names into disciplines and concrete technologies.

    The two lists are disjoint and map one-to-one onto ``ExtractedFact.skills`` and
    ``ExtractedFact.technologies``, so neither field repeats the other.

    Args:
        names: Canonical vocabulary names, typically from :func:`extract_skills`.

    Returns:
        ``(skills, technologies)``, each order-stable and deduplicated.
    """
    skills: list[str] = []
    technologies: list[str] = []
    seen: set[str] = set()
    for name in names:
        if not name or name in seen:
            continue
        seen.add(name)
        if skill_entity_kind(name) is EntityKind.SKILL:
            skills.append(name)
        else:
            technologies.append(name)
    return skills, technologies


# ======================================================================================
# Quantified impact
# ======================================================================================

#: Characters of surrounding text kept on either side of a numeric match, so a metric
#: reads as a phrase ("cut control-loop jitter to 35 us") rather than a bare number.
METRIC_CONTEXT_CHARS: Final[int] = 34

#: Punctuation trimmed from the ends of an extracted metric phrase.
_METRIC_TRIM: Final[str] = " \t-–—,;:•*|()[]"

#: Patterns for quantified outcomes. Ordered from most structured to least, though the
#: spans are merged afterwards so order only affects which pattern claims a tie.
_METRIC_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    # Rankings: "1st of 40 teams", "placed 2nd out of 300 entries", "top 5%".
    re.compile(
        r"\b(?:\d+(?:st|nd|rd|th)|first|second|third)\b(?:\s+place)?"
        r"\s+(?:out\s+of|of|among|in)\s+\d[\d,]*(?:\s+[A-Za-z]+){0,3}",
        re.IGNORECASE,
    ),
    re.compile(r"\btop\s+\d+(?:\.\d+)?\s*%", re.IGNORECASE),
    # Percentages and percentage points.
    re.compile(
        r"[<>~≈±]?\s*\d+(?:\.\d+)?\s*(?:%|percent\b|percentage\s+points\b|pp\b)",
        re.IGNORECASE,
    ),
    # Multipliers: "3x", "10×", "2.5x".
    re.compile(r"\b\d+(?:\.\d+)?\s*[x×](?![0-9A-Za-z])", re.IGNORECASE),
    # Currency, with optional magnitude suffix.
    re.compile(
        r"[$€£¥]\s?\d[\d,]*(?:\.\d+)?\s*(?:k\b|m\b|b\b|thousand\b|million\b|billion\b)?",
        re.IGNORECASE,
    ),
    # Latency, throughput, frequency and other engineering units.
    re.compile(
        r"\b\d[\d,]*(?:\.\d+)?\s*"
        r"(?:ms|µs|μs|us|ns|Hz|kHz|MHz|GHz|fps|GB/s|MB/s|KB/s|Gbps|Mbps|kbps|"
        r"req/s|rps|qps|ops/s|IOPS|rpm|dps|mAh|mW|kW|W|dB|°C|kg|mm|cm|km/h|"
        r"sec|secs|seconds|min|mins|minutes|LOC|sloc|dof|bit|bits)"
        r"(?![0-9A-Za-z])"
    ),
    # Counts with a magnitude suffix: "50k", "1.2M+", "3B".
    re.compile(r"\b\d[\d,]*(?:\.\d+)?\s*[kKmMbB]\+?(?![0-9A-Za-z])"),
    # Counts with a scale noun.
    re.compile(
        r"\b\d[\d,]*(?:\.\d+)?\s*(?:thousand|million|billion)?\s*"
        r"(?:users|students|customers|downloads|installs|devices|nodes|records|rows|"
        r"requests|commits|teams|members|engineers|contributors|hours|weeks|months|"
        r"years|lines\s+of\s+code|robots|boards|units|iterations|参)",
        re.IGNORECASE,
    ),
)


def _merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Collapse overlapping or touching ``(start, end)`` spans into disjoint ones.

    Args:
        spans: Match spans, in any order.

    Returns:
        Disjoint spans sorted by start offset.
    """
    if not spans:
        return []
    ordered = sorted(spans)
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def _metric_phrase(text: str, start: int, end: int) -> str:
    """Widen a numeric match into a readable phrase without crossing a line break.

    Args:
        text: The source text.
        start: Match start offset.
        end: Match end offset.

    Returns:
        The match plus up to :data:`METRIC_CONTEXT_CHARS` of context on each side, snapped
        to word boundaries and trimmed of leading/trailing punctuation.
    """
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    if line_end == -1:
        line_end = len(text)

    left = max(line_start, start - METRIC_CONTEXT_CHARS)
    if left > line_start:
        space = text.find(" ", left, start)
        if space != -1:
            left = space + 1
    right = min(line_end, end + METRIC_CONTEXT_CHARS)
    if right < line_end:
        space = text.rfind(" ", end, right)
        if space > end:
            right = space
    return text[left:right].strip(_METRIC_TRIM)


def extract_metrics(text: str) -> list[str]:
    """Return the quantified outcomes stated in *text*, each with a little context.

    Recognises percentages, multipliers (``3x``, ``10×``), currency, latency/throughput and
    other engineering units (``ms``, ``µs``, ``fps``, ``MHz``, ``GB/s``, ``req/s``), counts
    with scale suffixes or scale nouns, and competition rankings ("1st of 40 teams").

    A bare number is never returned: what makes a metric usable on a resume is the phrase
    around it, so each match is widened to word boundaries within its own line.

    Args:
        text: Free-form text.

    Returns:
        Metric phrases, deduplicated case-insensitively, in order of appearance.
    """
    if not text:
        return []
    spans = [
        match.span()
        for pattern in _METRIC_PATTERNS
        for match in pattern.finditer(text)
        if match.group(0).strip()
    ]
    phrases: list[str] = []
    seen: set[str] = set()
    for start, end in _merge_spans(spans):
        phrase = _metric_phrase(text, start, end)
        if not phrase or not any(character.isdigit() for character in phrase):
            continue
        key = " ".join(phrase.casefold().split())
        if key in seen:
            continue
        seen.add(key)
        phrases.append(" ".join(phrase.split()))
    return phrases


# ======================================================================================
# Dates
# ======================================================================================

#: Month name (or three-letter abbreviation) → month number.
_MONTH_NUMBERS: Final[dict[str, int]] = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

#: Season → the month a resume reader would understand it as. Northern-hemisphere academic
#: convention, which is what "Summer 2025 internship" means on every resume this product
#: will ever see.
_SEASON_MONTHS: Final[dict[str, int]] = {
    "spring": 3,
    "summer": 6,
    "fall": 9,
    "autumn": 9,
    "winter": 12,
}

#: Sentinel returned by the atom parser for "Present"/"Current"/"Ongoing". A distinct
#: object rather than ``None`` so the caller can tell "the source said it is ongoing" from
#: "the source said nothing", which are very different facts about a job.
_PRESENT: Final[str] = "\x00present"

#: One date, in any of the shapes resumes actually use. Alternatives are ordered so the
#: most specific wins: a full ``MM/DD/YYYY`` before ``MM/YYYY``, ``YYYY-MM`` before a bare
#: year. The month alternative of ``iso`` refuses two-digit values above 12, which is what
#: keeps ``2023-2024`` from being misread as "December 2023" plus a stray "24".
_DATE_ATOM: Final[re.Pattern[str]] = re.compile(
    r"(?P<present>present|currently|current|ongoing|to\s+date|today|now)"
    r"|(?P<monthyear>(?P<month>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
    r"\s+(?:of\s+)?(?P<month_year>(?:19|20)\d{2}))"
    r"|(?P<seasonyear>(?P<season>spring|summer|fall|autumn|winter)"
    r"\s+(?:of\s+)?(?P<season_year>(?:19|20)\d{2}))"
    r"|(?P<usdate>(?P<us_month>0?[1-9]|1[0-2])/(?:0?[1-9]|[12]\d|3[01])/(?P<us_year>(?:19|20)\d{2}))"
    r"|(?P<slash>(?P<slash_month>0?[1-9]|1[0-2])/(?P<slash_year>(?:19|20)\d{2}))"
    r"|(?P<iso>(?P<iso_year>(?:19|20)\d{2})[-/](?P<iso_month>0[1-9]|1[0-2]|[1-9])"
    r"(?:[-/]\d{1,2})?(?![0-9]))"
    r"|(?P<year>(?<![0-9])(?:19|20)\d{2}(?![0-9]))",
    re.IGNORECASE,
)

#: What may sit between the two halves of a date range and nothing else.
_RANGE_SEPARATOR: Final[re.Pattern[str]] = re.compile(
    r"\s*(?:-{1,2}|–|—|~|to|through|thru|until|till)\s*",
    re.IGNORECASE,
)


def _normalize_atom(match: re.Match[str]) -> str | None:
    """Convert one matched date atom to ``YYYY-MM``, ``YYYY``, or the present sentinel.

    Args:
        match: A match produced by :data:`_DATE_ATOM`.

    Returns:
        The normalised date, :data:`_PRESENT` for an open-ended period, or ``None`` when
        the match cannot be interpreted.
    """
    groups = match.groupdict()
    if groups.get("present"):
        return _PRESENT
    if groups.get("monthyear"):
        month = _MONTH_NUMBERS[groups["month"][:3].lower()]
        return f"{groups['month_year']}-{month:02d}"
    if groups.get("seasonyear"):
        month = _SEASON_MONTHS[groups["season"].lower()]
        return f"{groups['season_year']}-{month:02d}"
    if groups.get("usdate"):
        return f"{groups['us_year']}-{int(groups['us_month']):02d}"
    if groups.get("slash"):
        return f"{groups['slash_year']}-{int(groups['slash_month']):02d}"
    if groups.get("iso"):
        return f"{groups['iso_year']}-{int(groups['iso_month']):02d}"
    if groups.get("year"):
        return match.group("year")
    return None


def extract_dates(text: str) -> tuple[str | None, str | None]:
    """Return the ``(start, end)`` period *text* describes.

    Understands the shapes resumes and READMEs actually use — ``"Jan 2024 – Present"``,
    ``"2023-2024"``, ``"Summer 2025"``, ``"06/2023 - 08/2023"``, ``"2024-06"`` — and
    normalises each side to ``YYYY-MM`` when the month is known and ``YYYY`` when it is
    not. Precision is never invented: a source that said only "2023" yields ``"2023"``.

    Args:
        text: Free-form text.

    Returns:
        ``(start, end)``. ``end`` is ``None`` for ongoing work ("Present", "Current",
        "Ongoing") *and* when the source stated only one date, which are represented the
        same way because a resume renders both as an open period. ``(None, None)`` when no
        date is present.
    """
    if not text:
        return (None, None)

    atoms = [
        (match.start(), match.end(), normalized)
        for match in _DATE_ATOM.finditer(text)
        if (normalized := _normalize_atom(match)) is not None
    ]
    if not atoms:
        return (None, None)

    for index in range(len(atoms) - 1):
        _, left_end, left_value = atoms[index]
        right_start, _, right_value = atoms[index + 1]
        if left_value is _PRESENT:
            continue
        if not _RANGE_SEPARATOR.fullmatch(text[left_end:right_start]):
            continue
        return (left_value, None if right_value is _PRESENT else right_value)

    first = atoms[0][2]
    return (None, None) if first is _PRESENT else (first, None)


# ======================================================================================
# Bullet recovery
# ======================================================================================

#: Fewest words a line must have before it can become a fact. Below this it is a heading,
#: a label, or a fragment — never a claim worth putting on a resume.
MIN_BULLET_WORDS: Final[int] = 4

#: Longest a single recovered item may be. Beyond this it is a wall of prose that the
#: chunker, not the fact extractor, should be handling.
MAX_BULLET_CHARS: Final[int] = 600

_CODE_FENCE: Final[re.Pattern[str]] = re.compile(r"^\s*(?:```|~~~)")
_MARKDOWN_HEADING: Final[re.Pattern[str]] = re.compile(r"^\s*#{1,6}\s+")
_TABLE_ROW: Final[re.Pattern[str]] = re.compile(r"^\s*\|")
_HORIZONTAL_RULE: Final[re.Pattern[str]] = re.compile(r"^\s*(?:[-*_]\s*){3,}$")

#: Bullet markers: ASCII list characters, the common Unicode bullets, block quotes, and
#: numbered items. The lettered form requires a closing parenthesis specifically, because
#: ``a.`` would fire on "e.g. ..." at the start of a line.
_BULLET_MARKER: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?:[-*+•‣▪◦·▸►–—>]\s+|\(?\d{1,2}[.)]\s+|\(?[a-z]\)\s+)"
)

#: Markdown inline noise removed before a line is considered as a claim.
_MARKDOWN_IMAGE: Final[re.Pattern[str]] = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MARKDOWN_LINK: Final[re.Pattern[str]] = re.compile(r"\[([^\]]*)\]\((?:[^)]*)\)")
_MARKDOWN_EMPHASIS: Final[re.Pattern[str]] = re.compile(r"\*\*|__|`+")
_HTML_TAG: Final[re.Pattern[str]] = re.compile(
    r"</?(?:br|p|div|span|b|i|strong|em|img|a|ul|ol|li|h[1-6]|code|pre|table|tr|td|th)"
    r"\b[^>]*>",
    re.IGNORECASE,
)
_BARE_URL: Final[re.Pattern[str]] = re.compile(r"https?://\S+")
_WHITESPACE_RUN: Final[re.Pattern[str]] = re.compile(r"\s+")

#: Leading tokens that mark a line as a shell command or code rather than a claim.
_CODE_LINE_PREFIXES: Final[tuple[str, ...]] = (
    "pip ",
    "npm ",
    "yarn ",
    "pnpm ",
    "sudo ",
    "apt ",
    "brew ",
    "git clone",
    "cd ",
    "make ",
    "cmake ",
    "docker ",
    "curl ",
    "wget ",
    "export ",
    "source ",
    "./",
    "$ ",
    "> ",
    "python -m",
    "cargo ",
    "go run",
    "go build",
    "#include",
    "import ",
    "from ",
    "def ",
    "class ",
    "function ",
    "const ",
    "let ",
    "var ",
    "return ",
)


def _clean_inline(line: str) -> str:
    """Strip markdown and HTML noise from one line, keeping the words.

    Args:
        line: A raw line of source text.

    Returns:
        The line with images, link targets, emphasis markers, inline code fences and known
        HTML tags removed. Link *text* is kept — "[the CAN driver](docs/can.md)" is still a
        claim about a CAN driver.
    """
    cleaned = _MARKDOWN_IMAGE.sub("", line)
    cleaned = _MARKDOWN_LINK.sub(r"\1", cleaned)
    cleaned = _HTML_TAG.sub(" ", cleaned)
    cleaned = _MARKDOWN_EMPHASIS.sub("", cleaned)
    return cleaned


def _looks_like_code(item: str) -> bool:
    """Return whether an item is a command or a code fragment rather than a claim.

    Args:
        item: A recovered line.

    Returns:
        ``True`` when the line begins with a known command/keyword prefix or is dominated
        by code punctuation.
    """
    lowered = item.lstrip().casefold()
    if lowered.startswith(_CODE_LINE_PREFIXES):
        return True
    symbols = sum(item.count(character) for character in "{};=<>()[]")
    return symbols >= max(4, len(item) // 12)


def split_bullets(text: str) -> list[str]:
    """Recover the line-level items of *text*.

    Handles markdown (``-``, ``*``, ``+``), Unicode bullets (``•``, ``‣``, ``▪``, ``◦``),
    hyphen and dash lists, block quotes, and numbered or lettered lists — with the marker
    removed. Lines that wrap are rejoined onto the bullet they continue, headings are
    unwrapped to their text, fenced code blocks and table rows are skipped, and a run of
    unmarked lines becomes one paragraph item.

    Paragraphs are included deliberately: a README's opening sentence ("A bare-metal
    FreeRTOS stack for a 12-DOF quadruped") is exactly the kind of claim this engine
    exists to capture, and it is almost never written as a bullet.

    Args:
        text: Free-form text, typically markdown.

    Returns:
        The items, whitespace-collapsed, in document order. Never contains an empty string.
    """
    if not text:
        return []

    items: list[str] = []
    current: list[str] = []
    in_code_block = False

    def flush() -> None:
        """Emit the item being accumulated, if any."""
        if not current:
            return
        joined = _WHITESPACE_RUN.sub(" ", " ".join(current)).strip()
        current.clear()
        if joined:
            items.append(joined[:MAX_BULLET_CHARS])

    for raw_line in text.splitlines():
        if _CODE_FENCE.match(raw_line):
            flush()
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue

        line = _clean_inline(raw_line)
        if not line.strip() or _HORIZONTAL_RULE.match(line) or _TABLE_ROW.match(line):
            flush()
            continue

        heading = _MARKDOWN_HEADING.match(line)
        if heading:
            flush()
            current.append(line[heading.end() :])
            flush()
            continue

        marker = _BULLET_MARKER.match(line)
        if marker:
            flush()
            current.append(line[marker.end() :])
        else:
            current.append(line.strip())

    flush()
    return items


def _is_substantive(item: str) -> bool:
    """Return whether a recovered item is worth turning into a fact.

    Args:
        item: One output of :func:`split_bullets`.

    Returns:
        ``False`` for headings and labels (too few words), bare links, and shell or code
        lines; ``True`` otherwise.
    """
    without_urls = _BARE_URL.sub(" ", item).strip()
    if len(without_urls.split()) < MIN_BULLET_WORDS:
        return False
    if not any(len(word) >= 3 and word.isalpha() for word in without_urls.split()):
        return False
    return not _looks_like_code(without_urls)


# ======================================================================================
# Impact scoring
# ======================================================================================

MIN_IMPACT: Final[int] = 0
MAX_IMPACT: Final[int] = 100

#: Every bullet starts here; the modifiers below move it.
_IMPACT_BASE: Final[int] = 20

#: A measured outcome is the single strongest signal a bullet can carry, so it is worth
#: the most — but three metrics in one bullet is a wall of numbers, hence the cap.
_IMPACT_PER_METRIC: Final[int] = 12
_IMPACT_METRIC_CAP: Final[int] = 36

#: A strong verb in the first position means the sentence is about something the user did.
#: A strong verb anywhere still counts, but for less.
_IMPACT_LEADING_VERB: Final[int] = 12
_IMPACT_ANY_VERB: Final[int] = 6

#: Named technologies make a bullet searchable by an ATS and concrete to a human.
_IMPACT_PER_SKILL: Final[int] = 4
_IMPACT_SKILL_CAP: Final[int] = 16

_IMPACT_LEADERSHIP: Final[int] = 10
_IMPACT_SCALE: Final[int] = 6

#: Filler is penalised hard: it is not merely uninformative, it actively signals that the
#: user did not own the work.
_IMPACT_PER_FILLER: Final[int] = 15
_IMPACT_FILLER_CAP: Final[int] = 30

#: A bullet shorter than this says too little to rank against anything.
_IMPACT_SHORT_WORDS: Final[int] = 6
_IMPACT_SHORT_PENALTY: Final[int] = 20
_IMPACT_TERSE_WORDS: Final[int] = 10
_IMPACT_TERSE_PENALTY: Final[int] = 8

#: Splits a bullet into comparable word tokens for verb matching.
_WORD_TOKEN: Final[re.Pattern[str]] = re.compile(r"[A-Za-z][A-Za-z'-]*")


def score_impact(text: str, skills: Sequence[str], metrics: Sequence[str]) -> int:
    """Score how strong a claim is, 0-100, deterministically.

    Used to rank bullets when a tailored resume has to shrink to one page, and to break
    ties during retrieval. The heuristic is explainable on purpose — a user who asks "why
    was this bullet dropped?" deserves an answer better than "the model decided".

    Rewards: quantified outcomes, a strong action verb (most of all in first position),
    named technologies, ownership and leadership language, and evidence of real scale.
    Penalises: filler that describes proximity to work rather than ownership of it
    ("responsible for", "helped with", "worked on"), and bullets too short to say anything.

    Args:
        text: The claim.
        skills: Canonical vocabulary names evidenced by it, from :func:`extract_skills`.
        metrics: Quantified outcomes in it, from :func:`extract_metrics`.

    Returns:
        An integer in ``[0, 100]``.
    """
    if not text or not text.strip():
        return MIN_IMPACT

    lowered = text.casefold()
    words = _WORD_TOKEN.findall(lowered)
    score = _IMPACT_BASE

    score += min(len(metrics) * _IMPACT_PER_METRIC, _IMPACT_METRIC_CAP)

    if words and words[0] in ACTION_VERBS:
        score += _IMPACT_LEADING_VERB
    elif any(word in ACTION_VERBS for word in words):
        score += _IMPACT_ANY_VERB

    score += min(len(skills) * _IMPACT_PER_SKILL, _IMPACT_SKILL_CAP)

    if any(term in lowered for term in LEADERSHIP_TERMS):
        score += _IMPACT_LEADERSHIP
    if any(term in lowered for term in SCALE_TERMS):
        score += _IMPACT_SCALE

    fillers = sum(1 for phrase in FILLER_PHRASES if phrase in lowered)
    score -= min(fillers * _IMPACT_PER_FILLER, _IMPACT_FILLER_CAP)

    if len(words) < _IMPACT_SHORT_WORDS:
        score -= _IMPACT_SHORT_PENALTY
    elif len(words) < _IMPACT_TERSE_WORDS:
        score -= _IMPACT_TERSE_PENALTY

    return max(MIN_IMPACT, min(MAX_IMPACT, score))


# ======================================================================================
# Deduplication, shared with the ORM
# ======================================================================================


def normalize_fact_text(text: str) -> str:
    """Return the matching form of a fact's text.

    Delegates to :meth:`~app.models.knowledge.KnowledgeFact.normalize_text` — not a
    reimplementation of it. Two normalisations that drifted apart would make the same claim
    hash two different ways, and deduplication would silently stop working while appearing
    to run.

    Args:
        text: The claim as written.

    Returns:
        The normalised form stored in ``KnowledgeFact.normalized_text``.
    """
    return KnowledgeFact.normalize_text(text or "")


def fact_content_hash(fact: ExtractedFact) -> str:
    """Return the deduplication digest an :class:`ExtractedFact` will have as a row.

    Uses :meth:`~app.models.knowledge.KnowledgeFact.build_content_hash` with the same five
    identifying fields in the same order, so a hash computed here matches the stored row's
    ``content_hash`` exactly. That is what lets the indexer decide "insert or update?"
    before it has touched the database.

    Args:
        fact: The extracted claim.

    Returns:
        A 64-character lowercase SHA-256 hex digest.
    """
    return KnowledgeFact.build_content_hash(
        normalized_text=normalize_fact_text(fact.text),
        organization=fact.organization,
        role=fact.role,
        date_start=fact.date_start,
        date_end=fact.date_end,
    )


# ======================================================================================
# The rule-based path
# ======================================================================================

#: Confidence floor for a deterministically extracted fact. Below an LLM's typical output
#: because the rules cannot judge meaning — only shape.
_RULE_BASE_CONFIDENCE: Final[float] = 0.55
_RULE_METRIC_BONUS: Final[float] = 0.15
_RULE_SKILL_BONUS: Final[float] = 0.10
_RULE_MAX_CONFIDENCE: Final[float] = 0.90

#: Confidence assigned to an entity recovered straight from the vocabulary. High: the name
#: was matched against a curated list, so there is nothing to be uncertain about.
_RULE_ENTITY_CONFIDENCE: Final[float] = 0.80

#: Confidence assigned to a project entity inferred from a document title.
_RULE_PROJECT_CONFIDENCE: Final[float] = 0.60

#: Titles that are document structure, not project names.
_GENERIC_TITLES: Final[frozenset[str]] = frozenset(
    {
        "readme",
        "read me",
        "documentation",
        "docs",
        "introduction",
        "intro",
        "about",
        "overview",
        "getting started",
        "quick start",
        "quickstart",
        "installation",
        "install",
        "usage",
        "table of contents",
        "contents",
        "contributing",
        "license",
        "changelog",
        "features",
        "requirements",
        "setup",
        "build",
        "roadmap",
        "faq",
        "index",
        "home",
        "projects",
        "portfolio",
        "resume",
        "cv",
        "experience",
    }
)

#: Longest a detected project name may be before it is prose rather than a name.
_MAX_PROJECT_NAME_CHARS: Final[int] = 80

_SETEXT_HEADING: Final[re.Pattern[str]] = re.compile(r"^\s*(=+|-{3,})\s*$")
_LABELLED_PROJECT: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?:project|project\s+name|title)\s*[:\-—]\s*(?P<name>.+?)\s*$",
    re.IGNORECASE,
)
_TITLE_TRAILER: Final[re.Pattern[str]] = re.compile(r"\s*[-–—:|]\s.*$")


def _clean_title(candidate: str) -> str | None:
    """Reduce a heading to a usable project name, or reject it.

    Args:
        candidate: Raw heading text.

    Returns:
        The cleaned name, or ``None`` when it is generic document structure, empty, or too
        long to be a name.
    """
    cleaned = _clean_inline(candidate)
    cleaned = _BARE_URL.sub("", cleaned)
    cleaned = _TITLE_TRAILER.sub("", cleaned)
    cleaned = _WHITESPACE_RUN.sub(" ", cleaned).strip(" \t#*_-–—:|")
    if not cleaned or len(cleaned) > _MAX_PROJECT_NAME_CHARS:
        return None
    if cleaned.casefold() in _GENERIC_TITLES:
        return None
    return cleaned


def detect_project_name(text: str) -> str | None:
    """Infer the name of the project *text* describes, if it states one.

    Looks, in order, for an explicit ``Project: X`` label, a markdown ``# X`` heading, and
    a setext heading (a line underlined with ``===``). Generic structural headings —
    "README", "Overview", "Getting Started" — are rejected, because attaching every
    technology in a document to a node called "Installation" would poison the graph.

    Args:
        text: Free-form text, typically a README or a project page.

    Returns:
        The project name, or ``None`` when the document does not name itself.
    """
    if not text:
        return None
    lines = text.splitlines()

    for line in lines:
        labelled = _LABELLED_PROJECT.match(line)
        if labelled:
            name = _clean_title(labelled.group("name"))
            if name:
                return name

    for line in lines:
        if _MARKDOWN_HEADING.match(line):
            name = _clean_title(_MARKDOWN_HEADING.sub("", line, count=1))
            if name:
                return name

    for index, line in enumerate(lines[:-1]):
        if line.strip() and _SETEXT_HEADING.match(lines[index + 1]):
            name = _clean_title(line)
            if name:
                return name
    return None


def _rule_confidence(skills: Sequence[str], metrics: Sequence[str]) -> float:
    """Return the confidence for a deterministically extracted fact.

    Args:
        skills: Canonical names evidenced by the claim.
        metrics: Quantified outcomes in the claim.

    Returns:
        A value in ``[0.55, 0.90]``: corroboration by a recognised technology or a measured
        number is evidence that the line really is a claim and not stray prose.
    """
    confidence = _RULE_BASE_CONFIDENCE
    if metrics:
        confidence += _RULE_METRIC_BONUS
    if skills:
        confidence += _RULE_SKILL_BONUS
    return min(confidence, _RULE_MAX_CONFIDENCE)


def extract_facts_rule_based(
    text: str,
    *,
    kind: FactKind,
    organization: str | None = None,
    role: str | None = None,
    source_uri: str | None = None,
) -> list[ExtractedFact]:
    """Extract facts from *text* with no model, no network and no API key.

    The offline backbone of the knowledge engine and the fallback for every LLM failure.
    Recovers items with :func:`split_bullets`, discards headings, commands and fragments,
    and fills each surviving claim with the skills, technologies, metrics, dates and impact
    score the deterministic analysers can prove from the text itself.

    Dates fall back to the document's period when a bullet does not state its own, which is
    how "Jan 2024 – Aug 2024" in a section header ends up on every bullet under it.

    Args:
        text: Free-form text — a README, a job description, a resume section.
        kind: The :class:`~app.models.enums.FactKind` to assign to every extracted claim.
            The caller knows the context; the rules do not try to guess it.
        organization: Employer, school, club or project these claims belong to.
        role: Title held while they were true.
        source_uri: The document uri they came from, for provenance.

    Returns:
        The facts, in document order, deduplicated on :func:`fact_content_hash` so a
        repeated bullet produces one fact.
    """
    from app.knowledge.analyzers.base import ExtractedFact

    resolved_kind = FactKind(kind)
    document_start, document_end = extract_dates(text or "")

    facts: list[ExtractedFact] = []
    seen: set[str] = set()
    for item in split_bullets(text or ""):
        if not _is_substantive(item):
            continue

        names = extract_skills(item)
        skills, technologies = classify_skills(names)
        metrics = extract_metrics(item)
        start, end = extract_dates(item)
        if start is None:
            start, end = document_start, document_end

        fact = ExtractedFact(
            kind=resolved_kind,
            text=item,
            skills=skills,
            technologies=technologies,
            metrics=metrics,
            organization=organization,
            role=role,
            date_start=start,
            date_end=end,
            impact_score=score_impact(item, names, metrics),
            confidence=_rule_confidence(names, metrics),
            source_uri=source_uri,
        )
        digest = fact_content_hash(fact)
        if digest in seen:
            continue
        seen.add(digest)
        facts.append(fact)

    logger.debug(
        "extractor.rule_based_facts",
        facts=len(facts),
        source_uri=source_uri,
        kind=resolved_kind.value,
    )
    return facts


def extract_entities_rule_based(
    text: str,
) -> tuple[list[ExtractedEntity], list[ExtractedEdge]]:
    """Extract graph nodes and relationships from *text* with no model.

    Every vocabulary name found becomes an entity — a
    :attr:`~app.models.enums.EntityKind.SKILL` for a discipline, a
    :attr:`~app.models.enums.EntityKind.TECHNOLOGY` for a named tool. When the document
    names the project it describes (see :func:`detect_project_name`), that becomes a
    :attr:`~app.models.enums.EntityKind.PROJECT` node and every technology is linked to it
    with :attr:`~app.models.enums.RelationKind.USED_IN` — which is what later lets the
    resume engine answer "what did they actually build with Rust?".

    Args:
        text: Free-form text.

    Returns:
        ``(entities, edges)``. Both are empty when the text mentions nothing recognisable.
    """
    from app.knowledge.analyzers.base import ExtractedEdge, ExtractedEntity

    names = extract_skills(text or "")
    entities: list[ExtractedEntity] = [
        ExtractedEntity(
            kind=skill_entity_kind(name),
            name=name,
            aliases=list(SKILL_VOCABULARY.get(name, ())),
            confidence=_RULE_ENTITY_CONFIDENCE,
        )
        for name in names
    ]

    project = detect_project_name(text or "")
    if project is None:
        return entities, []

    entities.append(
        ExtractedEntity(
            kind=EntityKind.PROJECT,
            name=project,
            confidence=_RULE_PROJECT_CONFIDENCE,
        )
    )
    edges = [
        ExtractedEdge(
            source=(skill_entity_kind(name), name),
            target=(EntityKind.PROJECT, project),
            relation=RelationKind.USED_IN,
            evidence={"extractor": "rule_based", "project": project},
        )
        for name in names
    ]
    return entities, edges


# ======================================================================================
# The LLM-assisted path
# ======================================================================================

#: Fraction of a returned claim's content words that must also appear in the source before
#: the claim is believed. Below this the model wrote something the source does not say, and
#: golden rule #7 says that is not a fact — it is a fabrication.
MIN_SOURCE_OVERLAP: Final[float] = 0.5

#: Words ignored when measuring overlap: they appear in every sentence and would let a
#: hallucination pass on grammar alone.
_OVERLAP_STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        "a",
        "an",
        "and",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "in",
        "into",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "their",
        "this",
        "to",
        "was",
        "were",
        "with",
        "which",
        "while",
        "using",
        "used",
        "use",
        "we",
        "i",
        "my",
        "our",
        "also",
        "over",
        "than",
        "then",
        "so",
        "but",
        "not",
        "no",
        "all",
        "any",
    }
)

#: Longest slice of source text sent to the model. Longer inputs are chunked upstream by
#: :func:`~app.knowledge.analyzers.base.chunk_text`; this is a cost guard, not a strategy.
MAX_LLM_INPUT_CHARS: Final[int] = 16_000

#: Source kinds whose text a crawler fetched from a host the user does not control, and which
#: therefore pass through the ``docs/CONTRACTS.md`` §10b screen before extraction. Stored as
#: raw enum *values* so this module keeps no import of :class:`SourceKind` it does not
#: otherwise need.
_UNTRUSTED_SOURCE_KINDS: Final[frozenset[str]] = frozenset({"personal_website", "portfolio_page"})

#: Response ceiling for one extraction call.
_LLM_MAX_TOKENS: Final[int] = 4_096

#: Deterministic sampling, so the same document always extracts the same way and the cache
#: below is actually reusable.
_LLM_TEMPERATURE: Final[float] = 0.0

#: Cache namespace and lifetime for validated extraction payloads. Re-indexing an unchanged
#: document then costs nothing at all.
_CACHE_TAG: Final[str] = "knowledge_extract"
_CACHE_TTL_SECONDS: Final[int] = 7 * 24 * 60 * 60

#: An unrecognised skill longer or wordier than this is a sentence, not a technology.
_MAX_SKILL_WORDS: Final[int] = 3
_MAX_SKILL_CHARS: Final[int] = 40

#: Sentence punctuation inside a "skill" is proof it is prose.
_PROSE_MARKERS: Final[tuple[str, ...]] = (".", ",", ";", ":", "!", "?", " and ", " the ")

EXTRACT_FACTS_SYSTEM: Final[str] = (
    "You extract verifiable, atomic accomplishments from a person's own documents so they "
    "can be reused on a resume.\n"
    "Absolute rules:\n"
    "1. Never invent anything. Every fact must be supported by wording that is present in "
    "the source text. If the source does not say it, it does not exist.\n"
    "2. Never invent numbers, employers, job titles, or dates. Copy them from the source "
    "verbatim or leave the field null.\n"
    "3. One fact per claim. Split a compound sentence into separate facts.\n"
    "4. Keep the user's own vocabulary and specificity. Do not generalise "
    "'STM32H7' into 'a microcontroller'.\n"
    "5. Prefer claims with measurable outcomes, but never fabricate a measurement to "
    "create one.\n"
    "Reply with JSON only, matching the provided schema."
)

EXTRACT_FACTS_PROMPT: Final[str] = (
    "Extract the accomplishments stated in the source text below.\n\n"
    "Default fact kind: {kind}\n"
    "Known context (use these values; do not invent alternatives):\n"
    "  organization: {organization}\n"
    "  role: {role}\n\n"
    "For each fact provide:\n"
    "  text          - the claim, in the source's own words, one sentence\n"
    "  kind          - one of: {fact_kinds}\n"
    "  skills        - disciplines demonstrated (e.g. firmware, sensor fusion)\n"
    "  technologies  - named tools actually mentioned (e.g. FreeRTOS, PyTorch)\n"
    "  metrics       - quantified outcomes quoted exactly as the source wrote them\n"
    "  organization  - only if the source states it, else null\n"
    "  role          - only if the source states it, else null\n"
    "  date_start    - YYYY-MM or YYYY if the source states it, else null\n"
    "  date_end      - YYYY-MM or YYYY, or null for ongoing work\n"
    "  confidence    - 0.0 to 1.0\n\n"
    "SOURCE TEXT\n"
    "-----------\n"
    "{text}\n"
)

EXTRACT_ENTITIES_SYSTEM: Final[str] = (
    "You build a knowledge graph of what a person has worked with, from their own "
    "documents.\n"
    "Absolute rules:\n"
    "1. Only name entities that literally appear in the source text.\n"
    "2. Technologies are named tools, languages, frameworks, chips and platforms. Skills "
    "are disciplines and capabilities.\n"
    "3. Never infer a technology from a related one. 'PyTorch' does not imply 'CUDA'.\n"
    "4. Relationships must be supported by the text, not by general knowledge.\n"
    "Reply with JSON only, matching the provided schema."
)

EXTRACT_ENTITIES_PROMPT: Final[str] = (
    "Extract the entities and relationships stated in the source text below.\n\n"
    "Entity kinds: {entity_kinds}\n"
    "Relation kinds: {relation_kinds}\n\n"
    "For each entity provide name, kind, an optional one-line summary, and confidence "
    "(0.0-1.0).\n"
    "For each edge provide source_name, source_kind, target_name, target_kind, relation "
    "and weight. Both endpoints must be entities you listed.\n\n"
    "SOURCE TEXT\n"
    "-----------\n"
    "{text}\n"
)


def _string_array_schema(description: str) -> dict[str, Any]:
    """Return a JSON-schema fragment for an array of strings.

    Args:
        description: What the array holds, shown to the model.

    Returns:
        The schema fragment.
    """
    return {"type": "array", "items": {"type": "string"}, "description": description}


#: Strict response schema for the fact-extraction call. ``additionalProperties`` is closed
#: so a model that invents a field fails validation instead of smuggling data past it.
FACTS_JSON_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "kind": {"type": "string", "enum": FactKind.values()},
                    "skills": _string_array_schema("Disciplines demonstrated."),
                    "technologies": _string_array_schema("Named tools mentioned."),
                    "metrics": _string_array_schema("Quantified outcomes, quoted."),
                    "organization": {"type": ["string", "null"]},
                    "role": {"type": ["string", "null"]},
                    "date_start": {"type": ["string", "null"]},
                    "date_end": {"type": ["string", "null"]},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": ["text"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["facts"],
    "additionalProperties": False,
}

#: Strict response schema for the entity-extraction call.
ENTITIES_JSON_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "kind": {"type": "string", "enum": EntityKind.values()},
                    "summary": {"type": ["string", "null"]},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": ["name", "kind"],
                "additionalProperties": False,
            },
        },
        "edges": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source_name": {"type": "string"},
                    "source_kind": {"type": "string", "enum": EntityKind.values()},
                    "target_name": {"type": "string"},
                    "target_kind": {"type": "string", "enum": EntityKind.values()},
                    "relation": {"type": "string", "enum": RelationKind.values()},
                    "weight": {"type": "number"},
                },
                "required": [
                    "source_name",
                    "source_kind",
                    "target_name",
                    "target_kind",
                    "relation",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["entities"],
    "additionalProperties": False,
}


def _load_prompt(name: str, default: str) -> str:
    """Return a prompt from ``app.ai.prompts`` when that package provides one.

    ``docs/CONTRACTS.md`` §10 lists ``app/ai/prompts/`` in the layout but defines no API for
    it, so this looks for the two shapes a prompt package plausibly offers — a
    ``get_prompt(name)`` callable or a module-level constant — and otherwise uses the
    prompt defined in this module. Recorded in ``docs/OPEN_QUESTIONS.md``.

    Args:
        name: Prompt identifier, e.g. ``"extract_facts_system"``.
        default: The prompt to use when nothing overrides it.

    Returns:
        The prompt text.
    """
    try:
        from app.ai import prompts
    except ImportError:
        return default
    except Exception as exc:
        logger.warning("extractor.prompt_package_failed", prompt=name, error=str(exc))
        return default

    getter = getattr(prompts, "get_prompt", None)
    if callable(getter):
        try:
            rendered = getter(name)
        except Exception as exc:
            logger.debug("extractor.prompt_lookup_failed", prompt=name, error=str(exc))
        else:
            if isinstance(rendered, str) and rendered.strip():
                return rendered

    for attribute in (name.upper(), name):
        constant = getattr(prompts, attribute, None)
        if isinstance(constant, str) and constant.strip():
            return constant
    return default


def _content_tokens(text: str) -> set[str]:
    """Return the meaningful word tokens of *text*, for overlap measurement.

    Args:
        text: Any text.

    Returns:
        Lowercased word tokens with stopwords removed.
    """
    return {
        token for token in _WORD_TOKEN.findall(text.casefold()) if token not in _OVERLAP_STOPWORDS
    }


def _token_overlap(candidate: str, source: str) -> float:
    """Return the fraction of *candidate*'s content words that occur in *source*.

    The hallucination test. A faithful rewrite keeps the nouns; an invention does not.

    Args:
        candidate: Text the model produced.
        source: The text it was given.

    Returns:
        A value in ``[0.0, 1.0]``; ``0.0`` when the candidate has no content words at all.
    """
    candidate_tokens = _content_tokens(candidate)
    if not candidate_tokens:
        return 0.0
    return len(candidate_tokens & _content_tokens(source)) / len(candidate_tokens)


def _looks_like_prose(value: str) -> bool:
    """Return whether an unrecognised skill name is really a sentence.

    Args:
        value: A skill or technology name the vocabulary did not recognise.

    Returns:
        ``True`` when it is too long, too wordy, or carries sentence punctuation.
    """
    stripped = value.strip()
    if len(stripped) > _MAX_SKILL_CHARS or len(stripped.split()) > _MAX_SKILL_WORDS:
        return True
    lowered = f" {stripped.casefold()} "
    return any(marker in lowered for marker in _PROSE_MARKERS)


def _canonicalize_names(values: Any) -> list[str]:
    """Map model-supplied skill names onto the vocabulary, discarding prose.

    Args:
        values: Whatever the model put in a ``skills``/``technologies`` array.

    Returns:
        Canonical names first, then unrecognised-but-plausible names kept verbatim.
        Order-stable and deduplicated.
    """
    if not isinstance(values, (list, tuple)):
        return []
    resolved: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value.strip():
            continue
        canonical = canonical_skill(value)
        if canonical is None:
            if _looks_like_prose(value):
                continue
            canonical = value.strip()
        if canonical.casefold() in seen:
            continue
        seen.add(canonical.casefold())
        resolved.append(canonical)
    return resolved


def _grounded(value: Any, source_normalized: str) -> str | None:
    """Return *value* only when it actually occurs in the source text.

    Applied to organizations, roles and metrics: the model may *select* these, never
    *invent* them.

    Args:
        value: A candidate string from the model.
        source_normalized: The source text, run through
            :func:`~app.models.knowledge.normalize_for_matching`.

    Returns:
        The trimmed value, or ``None`` when it is absent from the source.
    """
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate:
        return None
    normalized = normalize_for_matching(candidate)
    if not normalized or normalized not in source_normalized:
        return None
    return candidate


class KnowledgeExtractor:
    """LLM-assisted extraction with a deterministic safety net underneath.

    Wraps a model call in validation strict enough that a hallucinating model degrades to
    the rule-based result rather than poisoning the knowledge base. Every returned item is
    checked against the source:

    * a fact whose wording overlaps the source by less than :data:`MIN_SOURCE_OVERLAP` is
      dropped and logged as ``extractor.hallucinated_fact``;
    * metrics, organizations and roles that do not occur in the source are dropped, and the
      caller's context values are used instead;
    * skills are canonicalised through :data:`SKILL_VOCABULARY`, and unrecognised names
      that read as prose are discarded;
    * confidences are clamped to ``[0, 1]``;
    * an edge whose endpoints are not both validated entities is dropped.

    **It never raises.** No API key, a timeout, a malformed response, an exhausted token
    budget, or a validated-but-empty result all end the same way: the rule-based path runs
    and ``extractor.fallback_rule_based`` is logged. That is what makes "works offline with
    zero API keys" a property of the system rather than an aspiration.

    Attributes:
        cache: Optional cache for validated payloads, keyed by content, so re-indexing an
            unchanged document costs nothing.
    """

    def __init__(self, llm: ModelPlugin | None = None, cache: Cache | None = None) -> None:
        """Construct an extractor.

        Args:
            llm: The model to use. When ``None`` the fast tier is resolved lazily from
                :func:`app.ai.llm.get_llm` on first use, and its absence is not an error —
                the rule-based path takes over.
            cache: Cache for validated model payloads. When ``None`` nothing is cached.
        """
        self._llm = llm
        self.cache = cache
        self._llm_resolution_attempted = llm is not None

    # -- model resolution ---------------------------------------------------------------

    def _resolve_llm(self) -> ModelPlugin | None:
        """Return the model to use, resolving it once and remembering the outcome.

        Returns:
            The model plugin, or ``None`` when none is available — an absent
            ``app.ai.llm``, a missing API key, or a constructor that refused.
        """
        if self._llm is not None or self._llm_resolution_attempted:
            return self._llm
        self._llm_resolution_attempted = True
        try:
            from app.ai.llm import get_llm

            self._llm = get_llm("fast")
        except Exception as exc:
            logger.info("extractor.llm_unavailable", error=str(exc))
            return None
        return self._llm

    # -- model invocation ---------------------------------------------------------------

    async def _complete_json(
        self,
        llm: ModelPlugin,
        *,
        tag: str,
        system: str,
        prompt: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        """Run one JSON-mode completion, reading through the cache when one is configured.

        Args:
            llm: The model plugin.
            tag: Cache discriminator identifying which extraction this is.
            system: System prompt.
            prompt: User prompt.
            schema: The strict response schema.

        Returns:
            The model's parsed response.

        Raises:
            Exception: Whatever the model plugin raises. The caller converts any failure
                into a rule-based fallback.
        """
        cache = self.cache
        if cache is None:
            return await llm.complete_json(
                system=system,
                prompt=prompt,
                schema=schema,
                max_tokens=_LLM_MAX_TOKENS,
                temperature=_LLM_TEMPERATURE,
            )

        from app.cache.keys import NAMESPACES, make_key

        key = make_key(NAMESPACES.LLM, _CACHE_TAG, tag, getattr(llm, "name", ""), system, prompt)
        cached_payload = await cache.get(key)
        if isinstance(cached_payload, dict):
            return cached_payload

        payload = await llm.complete_json(
            system=system,
            prompt=prompt,
            schema=schema,
            max_tokens=_LLM_MAX_TOKENS,
            temperature=_LLM_TEMPERATURE,
        )
        if isinstance(payload, dict):
            await cache.set(key, payload, ttl=_CACHE_TTL_SECONDS)
        return payload

    # -- validation ---------------------------------------------------------------------

    def _validate_facts(
        self,
        payload: Any,
        *,
        source: str,
        kind: FactKind,
        organization: str | None,
        role: str | None,
        source_uri: str | None,
    ) -> list[ExtractedFact]:
        """Turn a model payload into facts, discarding everything unsupported by *source*.

        Args:
            payload: The parsed model response.
            source: The text the model was given.
            kind: Fact kind to use when the model did not supply a valid one.
            organization: Context organization, used unless the source itself names one.
            role: Context role, used unless the source itself names one.
            source_uri: Provenance uri copied onto every fact.

        Returns:
            The surviving facts, deduplicated on :func:`fact_content_hash`.
        """
        from app.knowledge.analyzers.base import ExtractedFact

        if not isinstance(payload, dict):
            return []
        items = payload.get("facts")
        if not isinstance(items, list):
            return []

        source_normalized = normalize_for_matching(source)
        document_start, document_end = extract_dates(source)

        facts: list[ExtractedFact] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            text = " ".join(text.split())

            overlap = _token_overlap(text, source)
            if overlap < MIN_SOURCE_OVERLAP:
                logger.warning(
                    "extractor.hallucinated_fact",
                    overlap=round(overlap, 3),
                    threshold=MIN_SOURCE_OVERLAP,
                    source_uri=source_uri,
                    text=text[:200],
                )
                continue

            names = _canonicalize_names(item.get("skills")) + _canonicalize_names(
                item.get("technologies")
            )
            canonical_names = [name for name in names if canonical_skill(name) is not None]
            skills, technologies = classify_skills(
                [canonical_skill(name) or name for name in names]
            )

            metrics = [
                metric
                for metric in _clean_strings(item.get("metrics"))
                if _grounded(metric, source_normalized) is not None
            ] or extract_metrics(text)

            fact_kind = FactKind.coerce(item.get("kind"), kind) or kind
            fact_organization = (
                _grounded(item.get("organization"), source_normalized) or organization
            )
            fact_role = _grounded(item.get("role"), source_normalized) or role
            date_start, date_end = self._validated_dates(item, source, document_start, document_end)

            fact = ExtractedFact(
                kind=fact_kind,
                text=text,
                skills=skills,
                technologies=technologies,
                metrics=metrics,
                organization=fact_organization,
                role=fact_role,
                date_start=date_start,
                date_end=date_end,
                impact_score=score_impact(text, canonical_names or names, metrics),
                confidence=_coerce_confidence(item.get("confidence")),
                source_uri=source_uri,
            )
            digest = fact_content_hash(fact)
            if digest in seen:
                continue
            seen.add(digest)
            facts.append(fact)
        return facts

    @staticmethod
    def _validated_dates(
        item: dict[str, Any],
        source: str,
        document_start: str | None,
        document_end: str | None,
    ) -> tuple[str | None, str | None]:
        """Accept model-supplied dates only when the source contains the same year.

        Args:
            item: One fact object from the model payload.
            source: The text the model was given.
            document_start: Period start parsed from the whole source.
            document_end: Period end parsed from the whole source.

        Returns:
            ``(date_start, date_end)``, falling back to the document's own period when the
            model's dates are not corroborated.
        """
        start = item.get("date_start")
        end = item.get("date_end")
        if isinstance(start, str) and start[:4].isdigit() and start[:4] in source:
            resolved_end = (
                end if isinstance(end, str) and end[:4].isdigit() and end[:4] in source else None
            )
            return (start.strip(), resolved_end.strip() if resolved_end else None)
        return (document_start, document_end)

    def _validate_entities(
        self,
        payload: Any,
        *,
        source: str,
    ) -> tuple[list[ExtractedEntity], list[ExtractedEdge]]:
        """Turn a model payload into graph nodes and edges, dropping ungrounded ones.

        Args:
            payload: The parsed model response.
            source: The text the model was given.

        Returns:
            ``(entities, edges)``. An edge whose endpoints are not both validated entities
            is discarded, so the graph can never gain a node by way of an edge.
        """
        from app.knowledge.analyzers.base import ExtractedEdge, ExtractedEntity

        if not isinstance(payload, dict):
            return [], []

        source_normalized = normalize_for_matching(source)
        entities: list[ExtractedEntity] = []
        by_name: dict[str, ExtractedEntity] = {}

        for item in payload.get("entities") or []:
            if not isinstance(item, dict):
                continue
            raw_name = item.get("name")
            if not isinstance(raw_name, str) or not raw_name.strip():
                continue
            canonical = canonical_skill(raw_name)
            if canonical is None:
                if _looks_like_prose(raw_name) or _grounded(raw_name, source_normalized) is None:
                    logger.debug("extractor.ungrounded_entity", name=raw_name[:80])
                    continue
                name = raw_name.strip()
                entity_kind = EntityKind.coerce(item.get("kind"), EntityKind.TECHNOLOGY)
            else:
                name = canonical
                entity_kind = skill_entity_kind(canonical)

            if name.casefold() in by_name:
                continue
            summary = item.get("summary")
            entity = ExtractedEntity(
                kind=entity_kind or EntityKind.TECHNOLOGY,
                name=name,
                summary=summary if isinstance(summary, str) and summary.strip() else None,
                aliases=list(SKILL_VOCABULARY.get(name, ())),
                confidence=_coerce_confidence(item.get("confidence")),
            )
            entities.append(entity)
            by_name[name.casefold()] = entity

        edges: list[ExtractedEdge] = []
        for item in payload.get("edges") or []:
            if not isinstance(item, dict):
                continue
            source_entity = by_name.get(str(item.get("source_name", "")).strip().casefold())
            target_entity = by_name.get(str(item.get("target_name", "")).strip().casefold())
            relation = RelationKind.coerce(item.get("relation"), None)
            if source_entity is None or target_entity is None or relation is None:
                continue
            if source_entity is target_entity:
                continue
            weight = item.get("weight")
            edges.append(
                ExtractedEdge(
                    source=source_entity.identity,
                    target=target_entity.identity,
                    relation=relation,
                    weight=float(weight) if isinstance(weight, (int, float)) else 1.0,
                    evidence={"extractor": "llm"},
                )
            )
        return entities, edges

    # -- public API ---------------------------------------------------------------------

    def _rule_based_result(
        self,
        text: str,
        *,
        kind: FactKind,
        organization: str | None,
        role: str | None,
        source_uri: str | None,
    ) -> AnalysisResult:
        """Run the fully deterministic path and wrap it in an :class:`AnalysisResult`.

        Args:
            text: The source text.
            kind: Fact kind for every extracted claim.
            organization: Employer/school/project context.
            role: Title context.
            source_uri: Provenance uri.

        Returns:
            The rule-based result.
        """
        from app.knowledge.analyzers.base import AnalysisResult

        entities, edges = extract_entities_rule_based(text)
        return AnalysisResult(
            facts=extract_facts_rule_based(
                text,
                kind=kind,
                organization=organization,
                role=role,
                source_uri=source_uri,
            ),
            entities=entities,
            edges=edges,
        )

    @staticmethod
    def _screen_web_text(
        body: str,
        details: dict[str, Any],
        *,
        source_uri: str | None,
    ) -> str:
        """Apply the §10b screen to text that came off the open web.

        Only web sources are screened. A résumé, a LinkedIn export or a project folder is
        material the *user* handed over: screening those would fight the product's whole
        purpose, and an adversary who can write to the user's own disk has already won.
        :attr:`~app.models.enums.SourceKind.PERSONAL_WEBSITE` and
        :attr:`~app.models.enums.SourceKind.PORTFOLIO_PAGE` are different — a crawler fetched
        them, and a page can be edited by whoever controls the host.

        Args:
            body: The already-stripped source text.
            details: The caller's context dictionary; ``source_kind`` selects the screen.
            source_uri: Provenance uri, for the log line.

        Returns:
            The screened text, or ``""`` when it scored
            :attr:`~app.ai.untrusted.InjectionRisk.HIGH` and must not be extracted from.
        """
        from app.ai.untrusted import InjectionRisk, sanitize_external_text

        raw_kind = details.get("source_kind")
        kind_value = getattr(raw_kind, "value", raw_kind)
        if kind_value not in _UNTRUSTED_SOURCE_KINDS:
            return body

        screened, verdict = sanitize_external_text(
            body,
            source=f"{kind_value}:{source_uri or 'unknown'}",
            max_chars=MAX_LLM_INPUT_CHARS,
        )
        if verdict.risk is InjectionRisk.HIGH:
            logger.warning(
                "extractor.untrusted_blocked",
                source_uri=source_uri,
                source_kind=kind_value,
                score=verdict.score,
                signals=verdict.signals,
            )
            return ""
        return screened

    async def extract(
        self,
        text: str,
        *,
        kind: FactKind,
        context: dict[str, Any] | None = None,
    ) -> AnalysisResult:
        """Extract facts, entities and edges from *text*.

        Tries the model first and validates everything it returns; falls back to the
        deterministic path on any failure. Never raises.

        Args:
            text: The source text to extract from.
            kind: Default :class:`~app.models.enums.FactKind` for the extracted claims.
            context: Optional provenance and framing. Recognised keys — ``organization``,
                ``role``, ``source_uri``, ``source_kind`` — are attached to every fact;
                anything else is ignored, so a caller may pass a richer dictionary safely.
                ``source_kind`` additionally selects the ``docs/CONTRACTS.md`` §10b screen:
                a page fetched from the open web is untrusted input, a résumé the user handed
                us is not.

        Returns:
            The extracted knowledge. ``documents`` is always empty: this operates on text an
            analyzer already extracted, and the document is that analyzer's to emit. Web text
            that scored :attr:`~app.ai.untrusted.InjectionRisk.HIGH` yields an empty result
            and never reaches the model: an injection in a crawled page would otherwise write
            itself into the knowledge graph as a fact, where golden rule #7 would then make it
            authoritative forever.
        """
        from app.knowledge.analyzers.base import AnalysisResult

        details = context or {}
        organization = _optional_string(details.get("organization"))
        role = _optional_string(details.get("role"))
        source_uri = _optional_string(details.get("source_uri"))
        resolved_kind = FactKind(kind)

        body = (text or "").strip()
        if not body:
            return AnalysisResult()

        body = self._screen_web_text(body, details, source_uri=source_uri)
        if not body:
            return AnalysisResult()

        def fallback(reason: str) -> AnalysisResult:
            """Run the rule-based path and record why the model path was abandoned."""
            logger.info(
                "extractor.fallback_rule_based",
                reason=reason,
                source_uri=source_uri,
                kind=resolved_kind.value,
            )
            return self._rule_based_result(
                body,
                kind=resolved_kind,
                organization=organization,
                role=role,
                source_uri=source_uri,
            )

        llm = self._resolve_llm()
        if llm is None:
            return fallback("no_model")

        excerpt = body[:MAX_LLM_INPUT_CHARS]
        try:
            facts_payload = await self._complete_json(
                llm,
                tag="facts",
                system=_load_prompt("extract_facts_system", EXTRACT_FACTS_SYSTEM),
                prompt=_load_prompt("extract_facts", EXTRACT_FACTS_PROMPT).format(
                    kind=resolved_kind.value,
                    fact_kinds=", ".join(FactKind.values()),
                    organization=organization or "unknown",
                    role=role or "unknown",
                    text=excerpt,
                ),
                schema=FACTS_JSON_SCHEMA,
            )
        except Exception as exc:
            return fallback(f"{type(exc).__name__}: {exc}"[:200])

        facts = self._validate_facts(
            facts_payload,
            source=body,
            kind=resolved_kind,
            organization=organization,
            role=role,
            source_uri=source_uri,
        )
        if not facts:
            # A model that returned nothing usable — including the offline null stub, whose
            # placeholder text cannot pass the grounding check — must not silently erase
            # knowledge the deterministic rules can still see.
            return fallback("no_validated_facts")

        try:
            entities_payload = await self._complete_json(
                llm,
                tag="entities",
                system=_load_prompt("extract_entities_system", EXTRACT_ENTITIES_SYSTEM),
                prompt=_load_prompt("extract_entities", EXTRACT_ENTITIES_PROMPT).format(
                    entity_kinds=", ".join(EntityKind.values()),
                    relation_kinds=", ".join(RelationKind.values()),
                    text=excerpt,
                ),
                schema=ENTITIES_JSON_SCHEMA,
            )
        except Exception as exc:
            logger.info(
                "extractor.fallback_rule_based",
                reason=f"entities:{type(exc).__name__}",
                source_uri=source_uri,
            )
            entities, edges = extract_entities_rule_based(body)
        else:
            entities, edges = self._validate_entities(entities_payload, source=body)
            if not entities:
                logger.info(
                    "extractor.fallback_rule_based",
                    reason="no_validated_entities",
                    source_uri=source_uri,
                )
                entities, edges = extract_entities_rule_based(body)

        logger.debug(
            "extractor.llm_extracted",
            facts=len(facts),
            entities=len(entities),
            edges=len(edges),
            source_uri=source_uri,
        )
        return AnalysisResult(facts=facts, entities=entities, edges=edges)


# ======================================================================================
# Small shared helpers
# ======================================================================================


def _clean_strings(values: Any) -> list[str]:
    """Return the non-empty strings of an arbitrary model-supplied array.

    Args:
        values: Whatever the model put in a string array.

    Returns:
        Trimmed strings, in order; empty when *values* is not a list.
    """
    if not isinstance(values, (list, tuple)):
        return []
    return [value.strip() for value in values if isinstance(value, str) and value.strip()]


def _coerce_confidence(value: Any) -> float:
    """Clamp a model-supplied confidence into ``[0, 1]``, defaulting when absent.

    Args:
        value: The raw value from the payload.

    Returns:
        A usable confidence; ``0.5`` when the model omitted it or sent something unusable.
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return 0.5
    return max(0.0, min(1.0, float(value)))


def _optional_string(value: Any) -> str | None:
    """Return a trimmed non-empty string, or ``None``.

    Args:
        value: Any context value.

    Returns:
        The trimmed string, or ``None`` when it is absent or blank.
    """
    if not isinstance(value, str):
        return None
    return value.strip() or None
