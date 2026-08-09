"""Text embeddings — the numeric substrate the knowledge engine retrieves against.

``docs/CONTRACTS.md`` §10 defines the shape: an :class:`Embedder` protocol with a single
``embed(texts)`` coroutine, three implementations, a cached :func:`get_embedder` factory,
and the two similarity helpers :func:`cosine` and :func:`top_k`.

Embeddings are the most re-computed artifact in ApplicantOS. A single re-index of a
GitHub account, a portfolio and a resume produces thousands of chunks, most of which are
byte-identical to the ones embedded an hour ago. :func:`embed_texts` is therefore the entry
point application code should use: it splits a batch into cache hits and misses via
:mod:`app.cache` (namespace ``embedding``, keyed by provider, model and text), sends only
the misses upstream, writes the results back, and reassembles everything in the caller's
original order. Duplicate texts inside one batch collapse to a single upstream item.

Three providers, selected by ``settings.embedding_provider``:

``openai``
    :class:`OpenAIEmbedder` — the hosted ``text-embedding-3`` family, in batches of
    :data:`OPENAI_BATCH_SIZE`, with bounded exponential backoff and a hard check that the
    returned width equals ``settings.embedding_dim`` (a silent width change corrupts every
    vector already in the store, so it is refused loudly).
``local``
    :class:`LocalEmbedder` — the same wire protocol pointed at ``settings.local_llm_base_url``
    (Ollama, LM Studio, vLLM, llama.cpp). Nothing leaves the machine.
``hashing``
    :class:`HashingEmbedder` — deterministic, offline, dependency-free, and the fallback
    whenever a configured provider has no credential or no SDK. It is not a placeholder:
    it combines four hashed feature families (unigrams, adjacent bigrams, subword character
    n-grams and a curated technical-domain lexicon) with sublinear term weighting and L2
    normalisation, which gives genuinely usable cosine similarity on the short technical
    text this product indexes — "embedded firmware in C++ on STM32" lands near "RTOS driver
    development for ARM Cortex-M" and far from "React dashboard with Tailwind", even though
    the first two share no word at all.

Every bucket comes from a :func:`hashlib.blake2b` digest, never from Python's built-in
``hash()``: the latter is salted per process, so a cached vector written before a restart
would silently stop matching the vector computed after it. Determinism across processes and
across days is a correctness requirement here, not a nicety.
"""

from __future__ import annotations

import abc
import asyncio
import hashlib
import math
import random
import re
import threading
from importlib.util import find_spec
from itertools import pairwise
from typing import TYPE_CHECKING, Any, Final, Protocol, runtime_checkable

import structlog

from app.ai.llm import (
    MAX_RETRY_AFTER_SECONDS,
    RETRY_BASE_DELAY_SECONDS,
    RETRY_JITTER_RATIO,
    RETRY_MAX_DELAY_SECONDS,
    classify_error,
)
from app.cache import NAMESPACES, get_cache, make_key

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from collections.abc import Iterable, Mapping, Sequence

    from app.config.settings import Settings

__all__ = [
    "BIGRAM_WEIGHT",
    "CONCEPT_WEIGHT",
    "DEFAULT_TOP_K",
    "DOMAINS",
    "DOMAIN_LEXICON",
    "EMBEDDING_CACHE_TTL_SECONDS",
    "HASHING_ALGORITHM_VERSION",
    "HASHING_PROVIDER",
    "LOCAL_PROVIDER",
    "MAX_INPUT_CHARS",
    "MIN_EMBEDDING_DIM",
    "OPENAI_BATCH_SIZE",
    "OPENAI_PROVIDER",
    "PROVIDER_API_KEY_FIELDS",
    "STOPWORDS",
    "SUBWORD_MAX_N",
    "SUBWORD_MIN_N",
    "SUBWORD_WEIGHT",
    "UNIGRAM_WEIGHT",
    "BaseEmbedder",
    "Embedder",
    "EmbeddingDimensionMismatch",
    "EmbeddingError",
    "HashingEmbedder",
    "LocalEmbedder",
    "OpenAIEmbedder",
    "build_embedder",
    "content_tokens",
    "cosine",
    "domains_for",
    "embed_texts",
    "embedder_identity",
    "get_embedder",
    "l2_normalize",
    "reset_embedder_cache",
    "resolve_embedding_provider",
    "tokenize",
    "top_k",
]

logger = structlog.get_logger(__name__)


# ======================================================================================
# Constants
# ======================================================================================

#: Registered provider names, matching ``settings.embedding_provider``.
OPENAI_PROVIDER: Final[str] = "openai"
LOCAL_PROVIDER: Final[str] = "local"
HASHING_PROVIDER: Final[str] = "hashing"

#: ``settings`` field holding the API key each provider needs. A provider absent from this
#: mapping (``local``, ``hashing``) needs no credential.
PROVIDER_API_KEY_FIELDS: Final[dict[str, str]] = {OPENAI_PROVIDER: "openai_api_key"}

#: Distributions whose absence forces the offline fallback, per provider. Checked with
#: :func:`importlib.util.find_spec`, which does not execute the package.
PROVIDER_REQUIRED_DISTRIBUTIONS: Final[dict[str, str]] = {
    OPENAI_PROVIDER: "openai",
    LOCAL_PROVIDER: "openai",
}

#: Inputs per request to the OpenAI embeddings endpoint. The API accepts more, but a large
#: batch is a large blast radius: one malformed input fails the whole request, and a
#: transient failure re-sends everything in it. Ninety-six is the size the SDK's own
#: examples use and keeps a request comfortably inside the payload limit.
OPENAI_BATCH_SIZE: Final[int] = 96

#: Characters kept from a single input. ``text-embedding-3`` accepts 8191 tokens; at the
#: four-characters-per-token heuristic used throughout ApplicantOS that is ~32 700
#: characters, and the chunker upstream produces far less. Anything longer is truncated
#: rather than allowed to fail an entire batch.
MAX_INPUT_CHARS: Final[int] = 30_000

#: How long an embedding stays cached. Deliberately far longer than
#: ``settings.cache_default_ttl``: an embedding is a pure function of (provider, model,
#: text), so it can never go stale — only the model name changing can invalidate it, and
#: that changes the key.
EMBEDDING_CACHE_TTL_SECONDS: Final[int] = 30 * 24 * 60 * 60

#: Default number of neighbours :func:`top_k` returns.
DEFAULT_TOP_K: Final[int] = 10

#: Smallest sane hashed-embedding width. Below this, bucket collisions dominate and cosine
#: similarity stops meaning anything.
MIN_EMBEDDING_DIM: Final[int] = 32

#: Bytes of BLAKE2b digest consumed per feature. Eight gives a 64-bit integer: ample for
#: bucket selection at any realistic width, with a spare high bit for the sign.
_DIGEST_BYTES: Final[int] = 8

#: Bit index of the sign bit inside that integer. The bucket is taken from the low bits
#: (``value % dim``) and the sign from the highest, so the two are uncorrelated.
_SIGN_BIT: Final[int] = _DIGEST_BYTES * 8 - 1

#: BLAKE2b personalisation strings, one per feature family. Personalisation makes each
#: family an independent hash function, so a unigram and a subword n-gram that happen to
#: share text do not pile into the same bucket. Max 16 bytes (``blake2b.PERSON_SIZE``).
_PERSON_UNIGRAM: Final[bytes] = b"aos-emb-uni-v1"
_PERSON_BIGRAM: Final[bytes] = b"aos-emb-big-v1"
_PERSON_SUBWORD: Final[bytes] = b"aos-emb-sub-v1"
_PERSON_CONCEPT: Final[bytes] = b"aos-emb-con-v1"

#: Relative weight of each feature family before L2 normalisation.
#:
#: Unigrams carry the identity of the text. Bigrams add the little word-order signal that
#: distinguishes "unit test" from "test unit". Subword n-grams give morphological
#: robustness, so ``STM32H7`` is near ``STM32`` and ``FreeRTOS`` near ``RTOS``. Concept
#: tags project a token onto the technical domains it belongs to, which is what lets two
#: descriptions of the same work match when they share no vocabulary — the single most
#: valuable signal for this product, and the reason this embedder is usable offline.
UNIGRAM_WEIGHT: Final[float] = 1.0
BIGRAM_WEIGHT: Final[float] = 0.45
SUBWORD_WEIGHT: Final[float] = 0.22
CONCEPT_WEIGHT: Final[float] = 0.80

#: Inclusive range of character n-gram lengths taken inside each token.
SUBWORD_MIN_N: Final[int] = 3
SUBWORD_MAX_N: Final[int] = 4

#: Tokens shorter than this contribute no subword n-grams — the padded form would just
#: reproduce the token itself.
_SUBWORD_MIN_TOKEN_CHARS: Final[int] = 4

#: Token boundary markers, so a subword n-gram knows whether it sits at the start or end
#: of a word. ``^rto`` (start of "rtos") and ``rto`` (middle of "certoso") are different
#: features, which is the point.
_SUBWORD_START: Final[str] = "^"
_SUBWORD_END: Final[str] = "$"

#: Separator joining the two halves of a bigram feature. A unit separator can never occur
#: inside a token, so ``("a b", "c")`` and ``("a", "b c")`` cannot collide.
_BIGRAM_SEPARATOR: Final[str] = "\x1f"

#: Version of the hashed-embedding algorithm. **Bump this whenever the tokenizer, the
#: weights, the feature families or :data:`DOMAIN_LEXICON` change**: it is part of the
#: model identity, so bumping it invalidates every cached vector rather than mixing old and
#: new geometry in one vector store.
HASHING_ALGORITHM_VERSION: Final[str] = "v1"

#: Batch size above which :meth:`HashingEmbedder.embed` moves the work to a worker thread,
#: so a full re-index does not stall the event loop.
_HASHING_THREAD_THRESHOLD: Final[int] = 64

#: Jitter source for retry backoff. Unseeded and independent of the global :mod:`random`
#: state, which the deterministic null model relies on being undisturbed.
_JITTER_RNG: Final[random.SystemRandom] = random.SystemRandom()


# ======================================================================================
# Errors
# ======================================================================================


class EmbeddingError(RuntimeError):
    """An embedding could not be produced.

    Attributes:
        provider: The embedding provider that failed, when known.
        model: The embedding model the call targeted, when known.
    """

    def __init__(
        self, message: str, *, provider: str | None = None, model: str | None = None
    ) -> None:
        """Record the failure together with the provider that produced it.

        Args:
            message: Human-readable explanation.
            provider: The provider name.
            model: The model identifier.
        """
        super().__init__(message)
        self.provider = provider
        self.model = model


class EmbeddingDimensionMismatch(EmbeddingError):
    """The provider returned vectors of a width other than ``settings.embedding_dim``.

    Not recoverable at runtime and never silently coerced. The vector store's columns and
    indexes are built for one width, and mixing widths — or truncating to fit — produces
    a store whose similarity scores are quietly meaningless.

    Attributes:
        expected: The width ``settings.embedding_dim`` promises.
        actual: The width the provider actually returned.
    """

    def __init__(
        self,
        *,
        expected: int,
        actual: int,
        provider: str | None = None,
        model: str | None = None,
    ) -> None:
        """Describe the mismatch and how to fix it.

        Args:
            expected: Configured width.
            actual: Returned width.
            provider: The provider name.
            model: The model identifier.
        """
        super().__init__(
            f"embedding model {model!r} returned {actual}-dimensional vectors but "
            f"EMBEDDING_DIM is {expected}; set EMBEDDING_DIM={actual} (and re-index, "
            f"because existing vectors are {expected}-dimensional) or choose a model "
            f"whose width is {expected}",
            provider=provider,
            model=model,
        )
        self.expected = expected
        self.actual = actual


# ======================================================================================
# Tokenisation
# ======================================================================================

#: One token: a run of letters/digits, optionally continued across the punctuation that
#: appears *inside* technical identifiers, and optionally ending in ``+`` or ``#``.
#:
#: This is what keeps ``c++``, ``c#``, ``node.js``, ``scikit-learn``, ``cortex-m``,
#: ``ros2``, ``i2c`` and ``ci/cd`` as single tokens instead of shredding them into
#: meaningless fragments. ``[^\W_]`` is "word character except underscore", so it is
#: Unicode-aware without treating ``snake_case`` as one blob.
_TOKEN_PATTERN: Final[re.Pattern[str]] = re.compile(r"[^\W_]+(?:[+#._\-/][^\W_]+)*[+#]*")

#: English function words, removed before features are built. They cost norm and add no
#: signal. Deliberately **excluding** anything that names a technology: ``c``, ``r``,
#: ``go``, ``rust``, ``d`` and ``it`` (IT) are absent even where a generic stopword list
#: would include them.
STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        "a",
        "about",
        "above",
        "after",
        "again",
        "against",
        "all",
        "am",
        "an",
        "and",
        "any",
        "are",
        "aren",
        "as",
        "at",
        "be",
        "because",
        "been",
        "before",
        "being",
        "below",
        "between",
        "both",
        "but",
        "by",
        "can",
        "cannot",
        "could",
        "couldn",
        "did",
        "didn",
        "do",
        "does",
        "doesn",
        "doing",
        "don",
        "down",
        "during",
        "each",
        "either",
        "else",
        "etc",
        "even",
        "ever",
        "every",
        "few",
        "for",
        "from",
        "further",
        "had",
        "hadn",
        "has",
        "hasn",
        "have",
        "haven",
        "having",
        "he",
        "her",
        "here",
        "hers",
        "herself",
        "him",
        "himself",
        "his",
        "how",
        "however",
        "i",
        "if",
        "in",
        "into",
        "is",
        "isn",
        "its",
        "itself",
        "just",
        "let",
        "like",
        "ll",
        "may",
        "me",
        "might",
        "mine",
        "more",
        "most",
        "much",
        "must",
        "my",
        "myself",
        "neither",
        "no",
        "nor",
        "not",
        "now",
        "of",
        "off",
        "on",
        "once",
        "one",
        "only",
        "onto",
        "or",
        "other",
        "others",
        "ought",
        "our",
        "ours",
        "ourselves",
        "out",
        "over",
        "own",
        "per",
        "re",
        "same",
        "shall",
        "she",
        "should",
        "so",
        "some",
        "such",
        "than",
        "that",
        "the",
        "their",
        "theirs",
        "them",
        "themselves",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "though",
        "through",
        "thus",
        "to",
        "too",
        "under",
        "until",
        "up",
        "upon",
        "us",
        "ve",
        "very",
        "was",
        "wasn",
        "we",
        "were",
        "weren",
        "what",
        "when",
        "where",
        "whether",
        "which",
        "while",
        "who",
        "whom",
        "whose",
        "why",
        "will",
        "with",
        "within",
        "without",
        "won",
        "would",
        "wouldn",
        "you",
        "your",
        "yours",
        "yourself",
    }
)


def tokenize(text: str) -> list[str]:
    """Split *text* into lowercase word tokens.

    Case-folded rather than lowercased, so ``STRASSE``/``straße`` and other non-ASCII
    pairs normalise together. Technical identifiers survive intact — see
    :data:`_TOKEN_PATTERN`.

    Args:
        text: Any text.

    Returns:
        The tokens in source order, with duplicates kept (term frequency matters).
    """
    return _TOKEN_PATTERN.findall(text.casefold())


def content_tokens(text: str) -> list[str]:
    """Split *text* into meaningful tokens, dropping English function words.

    Args:
        text: Any text.

    Returns:
        The tokens of *text* with :data:`STOPWORDS` removed — or, when that would leave
        nothing at all (a title such as "About Me"), every token, so no text is ever
        reduced to a zero vector by stopword removal alone.
    """
    tokens = tokenize(text)
    kept = [token for token in tokens if token not in STOPWORDS]
    return kept or tokens


# ======================================================================================
# Technical domain lexicon
# ======================================================================================

#: Technical vocabulary grouped by the domain it belongs to, written domain-first because
#: that is how it is read and maintained; :data:`DOMAIN_LEXICON` is the inverted index
#: actually used at runtime. A term may appear under several domains, and should: ``cuda``
#: is machine learning *and* graphics, ``swift`` is mobile *and* a language.
#:
#: Every key is written exactly as :func:`tokenize` would emit it — case-folded, with
#: technical punctuation preserved (``c++``, ``node.js``, ``cortex-m``). Two-word keys are
#: matched against adjacent content-token pairs.
#:
#: The coverage mirrors :data:`app.knowledge.extractors.SKILL_VOCABULARY`'s domains
#: (embedded, robotics, ML/CV, web, tooling, games) because that is the vocabulary this
#: product's users write in, but the two serve different jobs: that one canonicalises a
#: skill name, this one places a word in a semantic neighbourhood.
_DOMAIN_TERMS: Final[dict[str, tuple[str, ...]]] = {
    "embedded": (
        "embedded",
        "firmware",
        "bootloader",
        "mcu",
        "microcontroller",
        "soc",
        "bare-metal",
        "baremetal",
        "rtos",
        "freertos",
        "zephyr",
        "threadx",
        "stm32",
        "stm32f4",
        "stm32h7",
        "esp32",
        "esp8266",
        "nrf52",
        "nrf53",
        "atmega",
        "avr",
        "arduino",
        "platformio",
        "stm32cubeide",
        "cubemx",
        "hal",
        "cmsis",
        "cortex",
        "cortex-m",
        "cortex-a",
        "arm",
        "risc-v",
        "msp430",
        "pic32",
        "teensy",
        "raspberry",
        "jetson",
        "interrupt",
        "interrupts",
        "isr",
        "dma",
        "watchdog",
        "eeprom",
        "i2c",
        "spi",
        "uart",
        "usart",
        "can",
        "canbus",
        "can-fd",
        "canopen",
        "modbus",
        "jtag",
        "swd",
        "openocd",
        "st-link",
        "segger",
        "gpio",
        "adc",
        "dac",
        "pwm",
        "peripheral",
        "peripherals",
        "datasheet",
        "register-level",
        "low-power",
        "yocto",
        "buildroot",
        "petalinux",
        "flashing",
        "toolchain",
        "embedded systems",
        "embedded linux",
        "embedded software",
        "device drivers",
        "device driver",
        "driver development",
        "bare metal",
        "real-time",
    ),
    "hardware": (
        "pcb",
        "kicad",
        "altium",
        "eagle",
        "schematic",
        "soldering",
        "smd",
        "reflow",
        "oscilloscope",
        "multimeter",
        "analyzer",
        "fpga",
        "verilog",
        "vhdl",
        "systemverilog",
        "vivado",
        "quartus",
        "xilinx",
        "altera",
        "asic",
        "rtl",
        "hdl",
        "electronics",
        "circuit",
        "circuits",
        "breadboard",
        "voltage",
        "analog",
        "sensor",
        "sensors",
        "imu",
        "accelerometer",
        "gyroscope",
        "magnetometer",
        "lidar",
        "encoder",
        "servo",
        "stepper",
        "bldc",
        "motor",
        "actuator",
        "relay",
        "transistor",
        "mosfet",
        "amplifier",
        "antenna",
        "rf",
        "ble",
        "bluetooth",
        "zigbee",
        "lora",
        "lorawan",
        "nfc",
        "battery",
        "enclosure",
        "machining",
        "cnc",
        "solidworks",
        "onshape",
        "autocad",
        "fusion",
        "cad",
        "tolerance",
        "fabrication",
        "printed circuit",
        "3d printing",
        "signal integrity",
        "power electronics",
    ),
    "robotics": (
        "robot",
        "robots",
        "robotic",
        "robotics",
        "ros",
        "ros2",
        "rclcpp",
        "rclpy",
        "gazebo",
        "moveit",
        "urdf",
        "tf2",
        "nav2",
        "slam",
        "odometry",
        "localization",
        "mapping",
        "kinematics",
        "manipulator",
        "gripper",
        "drone",
        "uav",
        "quadcopter",
        "rover",
        "autonomous",
        "teleoperation",
        "waypoint",
        "ekf",
        "kalman",
        "rplidar",
        "velodyne",
        "realsense",
        "pcl",
        "open3d",
        "sensor fusion",
        "path planning",
        "motion planning",
        "point cloud",
        "inverse kinematics",
        "robot operating",
    ),
    "control": (
        "pid",
        "lqr",
        "mpc",
        "setpoint",
        "damping",
        "state-space",
        "trajectory",
        "closed-loop",
        "open-loop",
        "feedforward",
        "actuation",
        "servo",
        "foc",
        "control theory",
        "motor control",
        "motion control",
        "control loop",
        "model predictive",
    ),
    "realtime": (
        "rtos",
        "freertos",
        "real-time",
        "realtime",
        "latency",
        "deterministic",
        "jitter",
        "scheduler",
        "scheduling",
        "preemptive",
        "interrupt",
        "microsecond",
        "millisecond",
        "throughput",
        "hard real-time",
        "worst-case",
    ),
    "systems": (
        "c",
        "c++",
        "rust",
        "assembly",
        "asm",
        "linux",
        "kernel",
        "unix",
        "posix",
        "pthread",
        "pthreads",
        "concurrency",
        "multithreading",
        "thread",
        "threads",
        "mutex",
        "semaphore",
        "atomic",
        "memory",
        "pointer",
        "pointers",
        "malloc",
        "allocator",
        "compiler",
        "linker",
        "gcc",
        "clang",
        "llvm",
        "makefile",
        "cmake",
        "ninja",
        "bazel",
        "gdb",
        "lldb",
        "valgrind",
        "profiling",
        "profiler",
        "optimization",
        "optimize",
        "syscall",
        "elf",
        "abi",
        "cross-compile",
        "segfault",
        "ubuntu",
        "debian",
        "bash",
        "shell",
        "systemd",
        "daemon",
        "operating system",
        "memory management",
        "low-level",
    ),
    "language": (
        "c",
        "c++",
        "c#",
        "python",
        "java",
        "javascript",
        "typescript",
        "rust",
        "go",
        "golang",
        "kotlin",
        "swift",
        "ruby",
        "php",
        "perl",
        "haskell",
        "scala",
        "elixir",
        "lua",
        "luau",
        "matlab",
        "dart",
        "julia",
        "fortran",
        "objective-c",
        "sql",
        "bash",
        "powershell",
        "vhdl",
        "verilog",
        "gdscript",
        "solidity",
        "assembly",
        "programming",
        "compiler",
        "syntax",
        "idiomatic",
    ),
    "ml": (
        "pytorch",
        "torch",
        "tensorflow",
        "keras",
        "jax",
        "scikit-learn",
        "sklearn",
        "xgboost",
        "lightgbm",
        "transformer",
        "transformers",
        "huggingface",
        "llm",
        "gpt",
        "bert",
        "embedding",
        "embeddings",
        "neural",
        "cnn",
        "rnn",
        "lstm",
        "dnn",
        "training",
        "trained",
        "inference",
        "dataset",
        "datasets",
        "overfitting",
        "regularization",
        "gradient",
        "backpropagation",
        "epoch",
        "epochs",
        "hyperparameter",
        "fine-tuning",
        "finetuning",
        "quantization",
        "onnx",
        "tensorrt",
        "cuda",
        "cudnn",
        "gpu",
        "reinforcement",
        "q-learning",
        "ppo",
        "rlhf",
        "classifier",
        "classification",
        "regression",
        "clustering",
        "supervised",
        "unsupervised",
        "inference",
        "machine learning",
        "deep learning",
        "neural network",
        "reinforcement learning",
        "transfer learning",
        "feature engineering",
    ),
    "cv": (
        "opencv",
        "cv2",
        "yolo",
        "yolov5",
        "yolov8",
        "image",
        "images",
        "vision",
        "segmentation",
        "detection",
        "camera",
        "calibration",
        "stereo",
        "depth",
        "pixel",
        "pixels",
        "augmentation",
        "imagenet",
        "resnet",
        "mediapipe",
        "tesseract",
        "ocr",
        "pose",
        "keypoint",
        "keypoints",
        "homography",
        "sift",
        "orb",
        "canny",
        "hough",
        "photogrammetry",
        "computer vision",
        "object detection",
        "image processing",
        "optical flow",
        "feature matching",
        "semantic segmentation",
    ),
    "data": (
        "pandas",
        "numpy",
        "scipy",
        "matplotlib",
        "seaborn",
        "plotly",
        "jupyter",
        "notebook",
        "dataframe",
        "etl",
        "analytics",
        "statistics",
        "statistical",
        "visualization",
        "bigquery",
        "spark",
        "hadoop",
        "airflow",
        "dbt",
        "warehouse",
        "csv",
        "parquet",
        "aggregation",
        "correlation",
        "outlier",
        "sampling",
        "data analysis",
        "data pipeline",
        "time series",
        "a/b",
    ),
    "frontend": (
        "react",
        "reactjs",
        "react.js",
        "next.js",
        "nextjs",
        "vue",
        "vuejs",
        "vue.js",
        "svelte",
        "sveltekit",
        "angular",
        "html",
        "css",
        "sass",
        "scss",
        "tailwind",
        "tailwindcss",
        "bootstrap",
        "jsx",
        "tsx",
        "dom",
        "component",
        "components",
        "hooks",
        "redux",
        "zustand",
        "vite",
        "webpack",
        "rollup",
        "babel",
        "eslint",
        "prettier",
        "responsive",
        "accessibility",
        "a11y",
        "animation",
        "framer",
        "shadcn",
        "storybook",
        "electron",
        "tanstack",
        "spa",
        "ssr",
        "dashboard",
        "dashboards",
        "frontend",
        "front-end",
        "ui",
        "viewport",
        "user interface",
        "design system",
        "single-page",
    ),
    "backend": (
        "api",
        "apis",
        "rest",
        "restful",
        "graphql",
        "grpc",
        "fastapi",
        "django",
        "flask",
        "express",
        "express.js",
        "node.js",
        "nodejs",
        "spring",
        "rails",
        "laravel",
        "microservice",
        "microservices",
        "endpoint",
        "endpoints",
        "middleware",
        "authentication",
        "authorization",
        "oauth",
        "jwt",
        "celery",
        "rabbitmq",
        "kafka",
        "webhook",
        "webhooks",
        "serverless",
        "backend",
        "back-end",
        "server",
        "asgi",
        "wsgi",
        "uvicorn",
        "gunicorn",
        "pydantic",
        "sqlalchemy",
        "orm",
        "throughput",
        "idempotent",
        "rest api",
        "message queue",
        "background job",
        "rate limiting",
    ),
    "web": (
        "http",
        "https",
        "html",
        "css",
        "javascript",
        "browser",
        "url",
        "rest",
        "websocket",
        "websockets",
        "cors",
        "dns",
        "cdn",
        "seo",
        "frontend",
        "backend",
        "fullstack",
        "full-stack",
        "web",
        "website",
        "webapp",
        "spa",
        "session",
        "cookie",
        "cookies",
        "ajax",
        "json",
        "web application",
        "web app",
    ),
    "mobile": (
        "android",
        "ios",
        "swift",
        "swiftui",
        "kotlin",
        "flutter",
        "dart",
        "xcode",
        "gradle",
        "mobile",
        "smartphone",
        "apk",
        "react native",
        "app store",
        "play store",
        "mobile app",
    ),
    "cloud": (
        "aws",
        "ec2",
        "s3",
        "lambda",
        "dynamodb",
        "cloudfront",
        "gcp",
        "azure",
        "cloud",
        "serverless",
        "terraform",
        "cloudformation",
        "iam",
        "vpc",
        "cloudwatch",
        "supabase",
        "firebase",
        "firestore",
        "heroku",
        "vercel",
        "netlify",
        "cloudflare",
        "kubernetes",
        "k8s",
        "autoscaling",
        "google cloud",
        "amazon web",
    ),
    "devops": (
        "docker",
        "dockerfile",
        "kubernetes",
        "k8s",
        "helm",
        "ci",
        "cd",
        "ci/cd",
        "jenkins",
        "gitlab",
        "pipeline",
        "pipelines",
        "deployment",
        "deploy",
        "deployed",
        "ansible",
        "terraform",
        "nginx",
        "prometheus",
        "grafana",
        "monitoring",
        "observability",
        "logging",
        "sre",
        "uptime",
        "rollout",
        "containerization",
        "container",
        "containers",
        "devops",
        "infrastructure",
        "provisioning",
        "orchestration",
        "github actions",
        "gitlab ci",
        "continuous integration",
        "continuous deployment",
        "infrastructure as",
    ),
    "database": (
        "sql",
        "postgres",
        "postgresql",
        "psql",
        "mysql",
        "sqlite",
        "mongodb",
        "mongo",
        "redis",
        "dynamodb",
        "cassandra",
        "elasticsearch",
        "pgvector",
        "index",
        "indexes",
        "indexing",
        "query",
        "queries",
        "transaction",
        "transactions",
        "schema",
        "migration",
        "migrations",
        "orm",
        "normalization",
        "sharding",
        "replication",
        "database",
        "databases",
        "db",
        "join",
        "joins",
        "rollback",
        "vector database",
        "full-text",
    ),
    "security": (
        "security",
        "encryption",
        "cryptography",
        "tls",
        "ssl",
        "oauth",
        "jwt",
        "authentication",
        "authorization",
        "vulnerability",
        "vulnerabilities",
        "penetration",
        "pentest",
        "owasp",
        "csrf",
        "xss",
        "injection",
        "firewall",
        "sandbox",
        "sandboxing",
        "threat",
        "exploit",
        "cve",
        "hardening",
        "access control",
        "secure boot",
        "zero trust",
    ),
    "networking": (
        "tcp",
        "udp",
        "ip",
        "http",
        "dns",
        "socket",
        "sockets",
        "packet",
        "packets",
        "wireshark",
        "tcpdump",
        "bandwidth",
        "protocol",
        "protocols",
        "ethernet",
        "wifi",
        "router",
        "routing",
        "mqtt",
        "zigbee",
        "lora",
        "ble",
        "network",
        "networking",
        "rpc",
        "grpc",
        "websocket",
        "latency",
        "handshake",
        "subnet",
        "network protocol",
        "packet capture",
    ),
    "testing": (
        "test",
        "tests",
        "testing",
        "pytest",
        "unittest",
        "jest",
        "vitest",
        "mocha",
        "cypress",
        "playwright",
        "selenium",
        "gtest",
        "googletest",
        "coverage",
        "tdd",
        "mock",
        "mocks",
        "mocking",
        "fixture",
        "fixtures",
        "assertion",
        "assertions",
        "qa",
        "validation",
        "verification",
        "benchmark",
        "benchmarks",
        "fuzzing",
        "regression",
        "hil",
        "debugging",
        "debugged",
        "reproducible",
        "unit test",
        "unit tests",
        "integration test",
        "test coverage",
        "hardware-in-the-loop",
        "test-driven",
    ),
    "gamedev": (
        "unity",
        "unity3d",
        "unreal",
        "godot",
        "roblox",
        "luau",
        "gdscript",
        "game",
        "games",
        "gameplay",
        "gamedev",
        "sprite",
        "sprites",
        "collision",
        "rigidbody",
        "multiplayer",
        "netcode",
        "fps",
        "engine",
        "npc",
        "procedural",
        "leaderboard",
        "playtesting",
        "monetization",
        "rojo",
        "game engine",
        "game design",
        "level design",
        "physics simulation",
        "unreal engine",
    ),
    "graphics": (
        "opengl",
        "vulkan",
        "directx",
        "metal",
        "webgl",
        "three.js",
        "shader",
        "shaders",
        "glsl",
        "hlsl",
        "rendering",
        "render",
        "rasterization",
        "raytracing",
        "mesh",
        "meshes",
        "texture",
        "textures",
        "shading",
        "gpu",
        "blender",
        "maya",
        "viewport",
        "vertex",
        "polygon",
        "lighting",
        "animation",
        "ray tracing",
        "compute shader",
        "real-time rendering",
    ),
    "design": (
        "figma",
        "design",
        "ux",
        "wireframe",
        "wireframes",
        "prototype",
        "prototyping",
        "typography",
        "layout",
        "branding",
        "accessibility",
        "usability",
        "mockup",
        "illustrator",
        "photoshop",
        "affinity",
        "palette",
        "spacing",
        "user research",
        "design system",
        "user experience",
        "visual design",
    ),
    "leadership": (
        "led",
        "lead",
        "leader",
        "leadership",
        "mentored",
        "mentoring",
        "mentor",
        "managed",
        "manager",
        "team",
        "teams",
        "captain",
        "president",
        "founder",
        "co-founder",
        "organized",
        "coordinated",
        "supervised",
        "directed",
        "chaired",
        "recruited",
        "onboarded",
        "delegated",
        "stakeholder",
        "stakeholders",
        "roadmap",
        "agile",
        "scrum",
        "sprint",
        "kanban",
        "prioritization",
        "hiring",
        "cross-functional",
        "project management",
        "team lead",
    ),
    "research": (
        "research",
        "researched",
        "paper",
        "papers",
        "publication",
        "published",
        "thesis",
        "dissertation",
        "conference",
        "journal",
        "ieee",
        "acm",
        "arxiv",
        "experiment",
        "experiments",
        "experimental",
        "hypothesis",
        "methodology",
        "literature",
        "peer-reviewed",
        "citation",
        "lab",
        "laboratory",
        "novel",
        "state-of-the-art",
        "ablation",
        "reproducibility",
        "research paper",
        "first author",
    ),
    "writing": (
        "documentation",
        "docs",
        "wrote",
        "writing",
        "readme",
        "tutorial",
        "tutorials",
        "blog",
        "article",
        "articles",
        "presentation",
        "presented",
        "report",
        "specification",
        "spec",
        "markdown",
        "latex",
        "proposal",
        "whitepaper",
        "authored",
        "editing",
        "technical writing",
        "api documentation",
        "design doc",
    ),
    "education": (
        "university",
        "college",
        "bachelor",
        "bachelors",
        "master",
        "masters",
        "phd",
        "degree",
        "gpa",
        "coursework",
        "semester",
        "graduated",
        "graduating",
        "major",
        "minor",
        "scholarship",
        "dean",
        "honors",
        "transcript",
        "student",
        "undergraduate",
        "graduate",
        "curriculum",
        "course",
        "courses",
        "seminar",
        "computer science",
        "electrical engineering",
        "mechanical engineering",
        "dean's list",
    ),
    "award": (
        "award",
        "awards",
        "awarded",
        "winner",
        "won",
        "finalist",
        "medal",
        "trophy",
        "champion",
        "championship",
        "recipient",
        "scholarship",
        "hackathon",
        "competition",
        "podium",
        "ranked",
        "placed",
        "distinction",
        "first place",
        "honorable mention",
    ),
}


def _invert_domain_terms(
    domain_terms: Mapping[str, Sequence[str]],
) -> dict[str, tuple[str, ...]]:
    """Invert the domain-first table into the term-first index used at runtime.

    Args:
        domain_terms: Domain name to the terms belonging to it.

    Returns:
        Each term mapped to the domains it belongs to, in the order the domains were
        declared, with duplicates removed.
    """
    inverted: dict[str, list[str]] = {}
    for domain, terms in domain_terms.items():
        for term in terms:
            bucket = inverted.setdefault(term, [])
            if domain not in bucket:
                bucket.append(domain)
    return {term: tuple(domains) for term, domains in inverted.items()}


#: Technical term to the domains it belongs to. The runtime index built from
#: :data:`_DOMAIN_TERMS`.
DOMAIN_LEXICON: Final[dict[str, tuple[str, ...]]] = _invert_domain_terms(_DOMAIN_TERMS)

#: Every domain name, sorted. Handy for diagnostics and tests.
DOMAINS: Final[tuple[str, ...]] = tuple(sorted(_DOMAIN_TERMS))


def domains_for(term: str) -> tuple[str, ...]:
    """Return the technical domains *term* belongs to.

    Args:
        term: A single token or a two-word phrase, case-insensitive.

    Returns:
        The domain names, or an empty tuple when the term is not in the lexicon.
    """
    return DOMAIN_LEXICON.get(term.casefold(), ())


# ======================================================================================
# Vector maths
# ======================================================================================


def l2_normalize(vector: Sequence[float]) -> list[float]:
    """Scale *vector* to unit length.

    Args:
        vector: The vector to normalise.

    Returns:
        A new list of the same width and unit L2 norm — or a copy of the input when its
        norm is zero, because there is no direction to preserve.
    """
    norm = math.sqrt(math.sumprod(vector, vector))
    if norm <= 0.0:
        return [float(value) for value in vector]
    # One reciprocal and N multiplications rather than N divisions: this runs once per
    # embedded chunk over a 1536-wide vector, and a full re-index has thousands of chunks.
    scale = 1.0 / norm
    return [float(value) * scale for value in vector]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Return the cosine similarity of two vectors.

    Args:
        a: First vector.
        b: Second vector.

    Returns:
        A value in ``[-1.0, 1.0]``; ``0.0`` when either vector is all zeros, which is what
        an empty or unembeddable text produces. The result is clamped, so accumulated
        floating-point error can never yield ``1.0000000000000002`` and trip a caller's
        range assertion.

    Raises:
        ValueError: If the vectors have different widths. Silently comparing a truncated
            prefix would return a plausible-looking number for what is always a bug.
    """
    if len(a) != len(b):
        raise ValueError(f"cannot compare vectors of different widths: {len(a)} and {len(b)}")
    # math.sumprod (3.12+) is a C-level dot product with extended intermediate precision:
    # both faster and more accurate than a Python-level sum of products, which matters
    # because ranking runs this over every candidate in a retrieval set.
    dot = math.sumprod(a, b)
    norm_a = math.sqrt(math.sumprod(a, a))
    norm_b = math.sqrt(math.sumprod(b, b))
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return max(-1.0, min(1.0, dot / (norm_a * norm_b)))


def top_k(
    query: Sequence[float],
    candidates: Sequence[Sequence[float]] | Mapping[Any, Sequence[float]],
    k: int = DEFAULT_TOP_K,
) -> list[tuple[Any, float]]:
    """Return the *k* candidates most similar to *query*, best first.

    Args:
        query: The query vector.
        candidates: Either a sequence of vectors — in which case results are labelled by
            positional index — or a mapping of identifier to vector, in which case they
            are labelled by that identifier.
        k: How many results to return. Values below one yield an empty list; values above
            the candidate count return everything.

    Returns:
        ``(label, score)`` pairs sorted by descending cosine similarity. Ties keep the
        candidates' original order, so the result is deterministic.

    Raises:
        ValueError: If any candidate's width differs from the query's.
    """
    if k < 1:
        return []
    pairs: Iterable[tuple[Any, Sequence[float]]] = (
        candidates.items() if hasattr(candidates, "items") else enumerate(candidates)
    )
    scored = [(label, cosine(query, vector)) for label, vector in pairs]
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored[:k]


# ======================================================================================
# The embedder protocol
# ======================================================================================


@runtime_checkable
class Embedder(Protocol):
    """Turns text into vectors, per ``docs/CONTRACTS.md`` §10.

    The protocol is deliberately one method wide so that a test double, a caching
    decorator or a third-party backend needs nothing else. Implementations shipped here
    additionally expose ``provider``, ``model`` and ``dimension``, which
    :func:`embed_texts` reads through :func:`embedder_identity` to build cache keys — a
    minimal implementation without them still works, keyed by its class name.
    """

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts.

        Args:
            texts: The texts to embed.

        Returns:
            One vector per input, in the same order and all of the same width.
        """
        ...


class BaseEmbedder(abc.ABC):
    """Shared plumbing for the built-in embedders: settings, identity and validation.

    Not a plugin. Embedding backends are chosen by ``settings.embedding_provider`` rather
    than discovered, and — unlike a model client or an ATS provider — carry no per-instance
    behaviour worth registering. :func:`get_embedder` is the only supported way to build
    one.

    Attributes:
        settings: The settings this embedder reads its model, width and credentials from.
    """

    #: Provider name, matching a value of ``settings.embedding_provider``.
    provider: str = "base"

    def __init__(self, settings: Settings | None = None) -> None:
        """Bind the embedder to a settings object.

        Args:
            settings: Application settings; the process-wide settings when omitted.

        Raises:
            ValueError: If ``settings.embedding_dim`` is below :data:`MIN_EMBEDDING_DIM`.
        """
        from app.config.settings import get_settings

        self.settings: Settings = settings if settings is not None else get_settings()
        dimension = int(self.settings.embedding_dim)
        if dimension < MIN_EMBEDDING_DIM:
            raise ValueError(f"EMBEDDING_DIM must be at least {MIN_EMBEDDING_DIM}, got {dimension}")
        self._dimension = dimension

    @property
    def dimension(self) -> int:
        """The width of every vector this embedder returns."""
        return self._dimension

    @property
    def model(self) -> str:
        """The model identifier that, with :attr:`provider`, identifies these vectors."""
        return str(self.settings.embedding_model)

    @abc.abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts, satisfying :class:`Embedder`.

        Args:
            texts: The texts to embed.

        Returns:
            One vector per input, in the same order and all of width
            :attr:`dimension`.
        """

    def _validate_batch(self, vectors: list[list[float]], expected_count: int) -> None:
        """Check a provider's reply for arity and width before it reaches the caller.

        Args:
            vectors: The vectors the provider returned.
            expected_count: How many inputs were sent.

        Raises:
            EmbeddingError: If the provider returned a different number of vectors than
                it was given inputs — which would silently misalign every vector with the
                wrong text.
            EmbeddingDimensionMismatch: If any vector's width is not :attr:`dimension`.
        """
        if len(vectors) != expected_count:
            raise EmbeddingError(
                f"embedding provider returned {len(vectors)} vectors for {expected_count} inputs",
                provider=self.provider,
                model=self.model,
            )
        for vector in vectors:
            if len(vector) != self._dimension:
                raise EmbeddingDimensionMismatch(
                    expected=self._dimension,
                    actual=len(vector),
                    provider=self.provider,
                    model=self.model,
                )

    def __repr__(self) -> str:
        """Return ``<ClassName provider/model dim=N>``."""
        return f"<{type(self).__name__} {self.provider}/{self.model} dim={self._dimension}>"


# ======================================================================================
# Hashing embedder — the offline path
# ======================================================================================


class HashingEmbedder(BaseEmbedder):
    """A deterministic, dependency-free embedder that works entirely offline.

    Four hashed feature families are accumulated into one ``settings.embedding_dim``-wide
    vector, each family using its own BLAKE2b personalisation so the families cannot
    collide with one another:

    1. **Unigrams** (:data:`UNIGRAM_WEIGHT`) — content tokens, weighted by sublinear term
       frequency ``1 + log(count)``, which stops a word repeated twelve times from
       dominating a paragraph.
    2. **Bigrams** (:data:`BIGRAM_WEIGHT`) — adjacent content-token pairs, the word-order
       signal that separates "test unit" from "unit test".
    3. **Subword n-grams** (:data:`SUBWORD_WEIGHT`) — character 3- and 4-grams inside each
       token, padded with boundary markers. This is what makes ``STM32H7`` similar to
       ``STM32`` and ``FreeRTOS`` to ``RTOS`` without either pair sharing a token.
    4. **Concept tags** (:data:`CONCEPT_WEIGHT`) — the technical domains each token or
       bigram belongs to, from :data:`DOMAIN_LEXICON`. Purely lexical embedders return
       ~0 similarity for two descriptions of the same work that share no vocabulary, which
       is the common case in résumé text; this family is what fixes that.

    Each feature is hashed to a bucket and a sign (the signed hashing trick): a shared
    feature always contributes ``+w₁w₂`` to the dot product, while two different features
    that collide contribute a randomly-signed term that cancels in expectation instead of
    inflating similarity. The finished vector is L2-normalised, so :func:`cosine` reduces
    to a dot product.

    Every bucket comes from :func:`hashlib.blake2b`, **never** from Python's ``hash()``,
    which is salted per process — a cached vector must mean the same thing tomorrow as it
    does today.

    What it is not: a learned semantic model. It will not know that "shipped" and
    "delivered" are synonyms unless the lexicon says so. What it is: a fast, stable,
    zero-dependency vector space in which technically-related text is genuinely close,
    which is what the offline requirement needs.
    """

    provider = HASHING_PROVIDER

    @property
    def model(self) -> str:
        """A synthetic model identifier that changes whenever the geometry does.

        Returns:
            ``hashing-<algorithm version>-<dimension>``. Both parts belong in the cache
            key: a re-tuned algorithm and a re-sized vector are different models, and
            reusing vectors across either would silently mix incompatible geometries.
        """
        return f"{HASHING_PROVIDER}-{HASHING_ALGORITHM_VERSION}-{self._dimension}"

    # -- feature accumulation ---------------------------------------------------------

    def _bucket(self, feature: str, person: bytes) -> tuple[int, float]:
        """Map one feature to a bucket index and a sign.

        Args:
            feature: The feature's text.
            person: The family's BLAKE2b personalisation string.

        Returns:
            ``(bucket, sign)`` where ``bucket`` is in ``[0, dimension)`` and ``sign`` is
            ``+1.0`` or ``-1.0``. The bucket is taken from the digest's low bits and the
            sign from its highest, so they are independent.
        """
        digest = hashlib.blake2b(
            feature.encode("utf-8"), digest_size=_DIGEST_BYTES, person=person
        ).digest()
        value = int.from_bytes(digest, "big")
        return value % self._dimension, 1.0 if (value >> _SIGN_BIT) & 1 else -1.0

    def _add_family(
        self,
        vector: list[float],
        features: Iterable[str],
        *,
        person: bytes,
        weight: float,
    ) -> None:
        """Accumulate one feature family into *vector* with sublinear term weighting.

        Args:
            vector: The accumulator, modified in place.
            features: The family's features, with repeats — they are counted here.
            person: The family's BLAKE2b personalisation string.
            weight: The family's relative weight.
        """
        if weight <= 0.0:
            return
        counts: dict[str, int] = {}
        for feature in features:
            counts[feature] = counts.get(feature, 0) + 1
        for feature, count in counts.items():
            bucket, sign = self._bucket(feature, person)
            vector[bucket] += sign * weight * (1.0 + math.log(count))

    @staticmethod
    def _subword_features(tokens: Sequence[str]) -> list[str]:
        """Return the character n-grams of *tokens*, with word-boundary markers.

        Args:
            tokens: Content tokens.

        Returns:
            Every ``n``-gram for ``n`` in :data:`SUBWORD_MIN_N`..:data:`SUBWORD_MAX_N`
            taken from each token padded as ``^token$``. Tokens shorter than
            :data:`_SUBWORD_MIN_TOKEN_CHARS` are skipped: their padded form is just the
            token again, which the unigram family already carries.
        """
        grams: list[str] = []
        for token in tokens:
            if len(token) < _SUBWORD_MIN_TOKEN_CHARS:
                continue
            padded = f"{_SUBWORD_START}{token}{_SUBWORD_END}"
            for size in range(SUBWORD_MIN_N, SUBWORD_MAX_N + 1):
                if len(padded) < size:
                    break
                grams.extend(
                    padded[start : start + size] for start in range(len(padded) - size + 1)
                )
        return grams

    @staticmethod
    def _concept_features(tokens: Sequence[str], bigrams: Sequence[str]) -> list[str]:
        """Return the domain tags *tokens* and *bigrams* map to.

        A two-word term is looked up as well as its parts, so "machine learning" tags the
        ``ml`` domain even though neither "machine" nor "learning" is in the lexicon
        alone.

        Args:
            tokens: Content tokens.
            bigrams: Adjacent content-token pairs, space-joined.

        Returns:
            One entry per (term, domain) hit, repeats included so that a text mentioning
            five embedded technologies scores more strongly on ``embedded`` than one
            mentioning a single technology.
        """
        tags: list[str] = []
        for term in (*tokens, *bigrams):
            tags.extend(DOMAIN_LEXICON.get(term, ()))
        return tags

    def embed_one(self, text: str) -> list[float]:
        """Embed a single text synchronously.

        The whole algorithm, in one place. :meth:`embed` is a batching wrapper over it.

        Args:
            text: The text to embed.

        Returns:
            A unit-length vector of width :attr:`~BaseEmbedder.dimension` — all zeros when
            *text* contains no tokens at all, which keeps :func:`cosine` well-defined
            without inventing a direction.
        """
        vector = [0.0] * self._dimension
        tokens = content_tokens(text)
        if not tokens:
            return vector

        bigrams = [f"{first} {second}" for first, second in pairwise(tokens)]

        self._add_family(vector, tokens, person=_PERSON_UNIGRAM, weight=UNIGRAM_WEIGHT)
        self._add_family(
            vector,
            (gram.replace(" ", _BIGRAM_SEPARATOR) for gram in bigrams),
            person=_PERSON_BIGRAM,
            weight=BIGRAM_WEIGHT,
        )
        self._add_family(
            vector,
            self._subword_features(tokens),
            person=_PERSON_SUBWORD,
            weight=SUBWORD_WEIGHT,
        )
        self._add_family(
            vector,
            self._concept_features(tokens, bigrams),
            person=_PERSON_CONCEPT,
            weight=CONCEPT_WEIGHT,
        )
        return l2_normalize(vector)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts offline.

        Args:
            texts: The texts to embed.

        Returns:
            One unit-length vector per input, in order.
        """
        if not texts:
            return []
        if len(texts) >= _HASHING_THREAD_THRESHOLD:
            # A full re-index embeds thousands of chunks; hand the CPU work to a worker
            # thread so the event loop keeps serving the API and the progress events.
            return await asyncio.to_thread(self._embed_all, texts)
        return self._embed_all(texts)

    def _embed_all(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed every text in *texts* synchronously.

        Args:
            texts: The texts to embed.

        Returns:
            One vector per input, in order.
        """
        return [self.embed_one(text) for text in texts]


# ======================================================================================
# Remote embedders
# ======================================================================================


def _backoff_delay(attempt: int, retry_after: float | None) -> float:
    """Return how long to wait before retry number *attempt* + 1.

    Mirrors :meth:`app.ai.llm.GuardedModelPlugin._backoff_delay` and shares its constants,
    so the completion path and the embedding path back off identically.

    Args:
        attempt: Zero-based index of the attempt that just failed.
        retry_after: Provider-requested cooldown in seconds, when it sent one.

    Returns:
        The delay in seconds — the provider's request when present (clamped to
        :data:`~app.ai.llm.MAX_RETRY_AFTER_SECONDS`), otherwise capped exponential
        backoff — plus jitter, so a fleet of workers rate-limited at the same instant does
        not retry in lockstep.
    """
    if retry_after is not None and retry_after > 0:
        base = min(retry_after, MAX_RETRY_AFTER_SECONDS)
    else:
        base = min(RETRY_BASE_DELAY_SECONDS * (2**attempt), RETRY_MAX_DELAY_SECONDS)
    return base + base * RETRY_JITTER_RATIO * _JITTER_RNG.random()


class OpenAIEmbedder(BaseEmbedder):
    """The hosted OpenAI embeddings endpoint, via the official async SDK.

    The SDK is imported lazily inside :meth:`_client`, so ApplicantOS imports and runs
    with the ``openai`` package absent.

    Two details matter operationally. First, requests go out in batches of
    :data:`OPENAI_BATCH_SIZE` with bounded exponential backoff on transient failures, using
    :func:`app.ai.llm.classify_error` to decide what is transient — the same duck-typed
    classification the completion path uses, so no SDK is imported to interpret an error.
    Second, the returned width is checked against ``settings.embedding_dim`` on every
    batch and a mismatch raises :class:`EmbeddingDimensionMismatch` rather than being
    padded, truncated or shrugged at.

    This class is also the base of :class:`LocalEmbedder`, exactly as
    :class:`~app.ai.models.openai.OpenAIModel` is the base of
    :class:`~app.ai.models.local.LocalModel`: every OpenAI-compatible server speaks this
    wire protocol, so the subclass overrides only credentials and base URL.
    """

    provider = OPENAI_PROVIDER

    #: Model families that accept a ``dimensions`` request parameter, which asks the API
    #: for a shortened embedding. Sending it means ``settings.embedding_dim`` is honoured
    #: instead of merely checked.
    DIMENSIONS_CAPABLE_PREFIXES: tuple[str, ...] = ("text-embedding-3",)

    def __init__(self, settings: Settings | None = None) -> None:
        """Create the embedder without touching the network or importing the SDK.

        Args:
            settings: Application settings; the process-wide settings when omitted.
        """
        super().__init__(settings)
        self._sdk_client: Any | None = None
        #: Cleared once the API rejects the ``dimensions`` parameter, so it is sent at
        #: most once against a deployment that does not support it.
        self._send_dimensions = self._supports_dimensions()

    # -- configuration hooks (overridden by LocalEmbedder) ----------------------------

    def _api_key(self) -> str:
        """Return the API key to authenticate with.

        Returns:
            ``settings.openai_api_key``, stripped.

        Raises:
            EmbeddingError: When no key is configured.
        """
        api_key = (self.settings.openai_api_key or "").strip()
        if not api_key:
            raise EmbeddingError(
                "OPENAI_API_KEY is not set; set it or set EMBEDDING_PROVIDER=hashing "
                "to embed offline",
                provider=self.provider,
                model=self.model,
            )
        return api_key

    def _base_url(self) -> str | None:
        """Return the API base URL, or ``None`` for the SDK default."""
        return None

    def _supports_dimensions(self) -> bool:
        """Return whether the configured model accepts a ``dimensions`` parameter."""
        return self.model.startswith(self.DIMENSIONS_CAPABLE_PREFIXES)

    # -- client -----------------------------------------------------------------------

    def _client(self) -> Any:
        """Return the shared ``AsyncOpenAI`` client, constructing it on first use.

        Returns:
            The SDK client.

        Raises:
            EmbeddingError: If the ``openai`` package is not installed or credentials are
                missing. Both are configuration problems, not transient ones.
        """
        if self._sdk_client is not None:
            return self._sdk_client

        try:
            import openai
        except ImportError as exc:  # pragma: no cover - exercised only without the SDK
            raise EmbeddingError(
                "the 'openai' package is not installed; install it or set "
                "EMBEDDING_PROVIDER=hashing to embed offline",
                provider=self.provider,
                model=self.model,
            ) from exc

        options: dict[str, Any] = {
            "api_key": self._api_key(),
            "timeout": float(self.settings.llm_timeout_seconds),
            "max_retries": 0,  # retries live in _embed_batch, not in two nested loops
        }
        base_url = self._base_url()
        if base_url:
            options["base_url"] = base_url

        self._sdk_client = openai.AsyncOpenAI(**options)
        return self._sdk_client

    # -- embedding --------------------------------------------------------------------

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts, in chunks of :data:`OPENAI_BATCH_SIZE`.

        Args:
            texts: The texts to embed.

        Returns:
            One vector per input, in order.

        Raises:
            EmbeddingError: On a provider failure that survived the retry policy, or when
                the reply's arity does not match the request's.
            EmbeddingDimensionMismatch: When the returned width is not
                ``settings.embedding_dim``.
        """
        if not texts:
            return []
        vectors: list[list[float]] = []
        for start in range(0, len(texts), OPENAI_BATCH_SIZE):
            window = texts[start : start + OPENAI_BATCH_SIZE]
            vectors.extend(await self._embed_batch([self._prepare_input(text) for text in window]))
        self._validate_batch(vectors, len(texts))
        return vectors

    def _prepare_input(self, text: str) -> str:
        """Return *text* in a form the API will accept.

        Args:
            text: One input.

        Returns:
            The text truncated to :data:`MAX_INPUT_CHARS`, and replaced by a single space
            when it is empty — the API rejects an empty string, and failing a whole batch
            over one blank chunk is not worth it. :func:`embed_texts` normally filters
            blanks out before they reach here.
        """
        stripped = text.strip()
        if not stripped:
            return " "
        if len(stripped) > MAX_INPUT_CHARS:
            logger.warning(
                "embeddings.input_truncated",
                provider=self.provider,
                model=self.model,
                characters=len(stripped),
                limit=MAX_INPUT_CHARS,
            )
            return stripped[:MAX_INPUT_CHARS]
        return stripped

    async def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        """Send one batch, retrying transient failures with backoff and jitter.

        Args:
            batch: At most :data:`OPENAI_BATCH_SIZE` prepared inputs.

        Returns:
            The batch's vectors, in request order.

        Raises:
            EmbeddingError: When every attempt failed, or the reply could not be read.
        """
        attempts = max(int(self.settings.llm_max_retries), 0) + 1

        for attempt in range(attempts):
            try:
                return self._read_vectors(await self._create(batch))
            except asyncio.CancelledError:
                raise
            except EmbeddingError:
                raise  # configuration problems are not retried
            except Exception as exc:
                # Bind outside the handler: Python unbinds `exc` when the block ends.
                cause: BaseException = exc
                classified = classify_error(exc, model=self.model)

            if not classified.transient or attempt == attempts - 1:
                logger.warning(
                    "embeddings.batch_failed",
                    provider=self.provider,
                    model=self.model,
                    size=len(batch),
                    attempt=attempt + 1,
                    attempts=attempts,
                    transient=classified.transient,
                    status_code=classified.status_code,
                    error=str(classified),
                )
                raise EmbeddingError(
                    f"embedding request failed after {attempt + 1} attempt(s): {classified}",
                    provider=self.provider,
                    model=self.model,
                ) from cause

            delay = _backoff_delay(attempt, classified.retry_after)
            logger.warning(
                "embeddings.retrying",
                provider=self.provider,
                model=self.model,
                attempt=attempt + 1,
                attempts=attempts,
                delay_seconds=round(delay, 3),
                error=str(classified),
            )
            await asyncio.sleep(delay)

        # Unreachable: the loop either returns or raises on its final iteration.
        raise EmbeddingError(
            "embedding request made no attempts", provider=self.provider, model=self.model
        )

    async def _create(self, batch: list[str]) -> Any:
        """Perform one embeddings request, dropping ``dimensions`` if it is rejected.

        Args:
            batch: The prepared inputs.

        Returns:
            The SDK response object.

        Raises:
            Exception: Whatever the SDK raised, for :func:`app.ai.llm.classify_error` to
                classify — except the specific ``dimensions`` rejection retried here.
        """
        client = self._client()
        request: dict[str, Any] = {"model": self.model, "input": batch}
        if not self._send_dimensions:
            return await client.embeddings.create(**request)
        try:
            return await client.embeddings.create(**request, dimensions=self._dimension)
        except Exception as exc:
            if not self._rejects_dimensions(exc):
                raise
            self._send_dimensions = False
            logger.info(
                "embeddings.dimensions_unsupported",
                provider=self.provider,
                model=self.model,
                detail="retrying without the dimensions parameter",
            )
        return await client.embeddings.create(**request)

    @staticmethod
    def _rejects_dimensions(exc: BaseException) -> bool:
        """Return whether *exc* is the API complaining about ``dimensions`` specifically.

        Args:
            exc: The exception the SDK raised.

        Returns:
            ``True`` when the message names the parameter and reports it as unknown or
            unsupported — the wording every OpenAI-compatible server uses.
        """
        message = str(exc).lower()
        return "dimensions" in message and any(
            marker in message
            for marker in ("unsupported", "unknown", "unrecognized", "not supported", "invalid")
        )

    def _read_vectors(self, response: Any) -> list[list[float]]:
        """Extract the vectors from an embeddings response, in request order.

        Args:
            response: The SDK response object.

        Returns:
            One vector per input.

        Raises:
            EmbeddingError: When the response carries no readable ``data`` array, which
                means the endpoint is not actually OpenAI-compatible.
        """
        data = getattr(response, "data", None)
        if data is None and isinstance(response, dict):
            data = response.get("data")
        if not isinstance(data, (list, tuple)):
            raise EmbeddingError(
                "embedding response has no 'data' array; the endpoint may not be OpenAI-compatible",
                provider=self.provider,
                model=self.model,
            )
        # The API documents `data` as ordered, but every item carries its own index and
        # honouring it costs one sort — cheap insurance against a proxy that reorders.
        items = sorted(data, key=lambda item: _item_index(item))
        return [_item_embedding(item, self.provider, self.model) for item in items]


class LocalEmbedder(OpenAIEmbedder):
    """An OpenAI-compatible embeddings server on the user's own machine.

    Points the same wire protocol at ``settings.local_llm_base_url`` — Ollama, LM Studio,
    vLLM or llama.cpp's server. Nothing leaves the machine and no key is needed, which
    makes this the privacy-preserving middle ground between hosted embeddings and the
    fully offline :class:`HashingEmbedder`.

    ``settings.embedding_model`` names the local model (for example
    ``nomic-embed-text``), and ``settings.embedding_dim`` must match that model's real
    width — 768 for ``nomic-embed-text``, not the hosted default of 1536. A mismatch is
    caught on the first batch and reported with the correct value to set.
    """

    provider = LOCAL_PROVIDER

    #: Placeholder credential. The SDK refuses to construct without a key and a local
    #: server never checks one.
    UNUSED_API_KEY: str = "not-needed"

    def _api_key(self) -> str:
        """Return the placeholder credential local servers ignore."""
        return self.UNUSED_API_KEY

    def _base_url(self) -> str:
        """Return the configured local endpoint.

        Returns:
            ``settings.local_llm_base_url`` with any trailing slash removed.

        Raises:
            EmbeddingError: When the setting is empty, which leaves nothing to call.
        """
        base_url = str(self.settings.local_llm_base_url).strip().rstrip("/")
        if not base_url:
            raise EmbeddingError(
                "LOCAL_LLM_BASE_URL is empty; set it or set EMBEDDING_PROVIDER=hashing",
                provider=self.provider,
                model=self.model,
            )
        return base_url

    def _supports_dimensions(self) -> bool:
        """Return ``False``: local servers serve one fixed width per model.

        Ollama and llama.cpp ignore or reject ``dimensions``, and a model's width is a
        property of its weights rather than a request parameter.
        """
        return False


def _item_index(item: Any) -> int:
    """Return an embedding item's position in the request.

    Args:
        item: One element of an embeddings response's ``data`` array.

    Returns:
        Its ``index`` field, or ``0`` when absent.
    """
    value = getattr(item, "index", None)
    if value is None and isinstance(item, dict):
        value = item.get("index")
    return int(value) if isinstance(value, (int, float)) else 0


def _item_embedding(item: Any, provider: str, model: str) -> list[float]:
    """Return an embedding item's vector as a list of floats.

    Args:
        item: One element of an embeddings response's ``data`` array.
        provider: Provider name, for the error message.
        model: Model identifier, for the error message.

    Returns:
        The vector.

    Raises:
        EmbeddingError: When the item carries no readable ``embedding`` array — for
            example when the server base64-encodes it, which this client does not request.
    """
    vector = getattr(item, "embedding", None)
    if vector is None and isinstance(item, dict):
        vector = item.get("embedding")
    if not isinstance(vector, (list, tuple)):
        raise EmbeddingError(
            "embedding response item has no 'embedding' array",
            provider=provider,
            model=model,
        )
    return [float(value) for value in vector]


# ======================================================================================
# The batching workhorse
# ======================================================================================


def embedder_identity(embedder: Embedder) -> tuple[str, str, int]:
    """Return the ``(provider, model, dimension)`` triple identifying *embedder*.

    These three values are what make a cached vector reusable: the same text embedded by
    a different provider, a different model, or at a different width is a different
    vector, and must not be served from one key.

    Args:
        embedder: Any embedder, including one that implements nothing but
            :meth:`Embedder.embed`.

    Returns:
        The triple, falling back to the class name and a zero width for a minimal
        implementation that exposes no identity of its own.
    """
    provider = str(getattr(embedder, "provider", "") or type(embedder).__name__)
    model = str(getattr(embedder, "model", "") or "")
    dimension = getattr(embedder, "dimension", 0)
    return provider, model, int(dimension) if isinstance(dimension, int) else 0


def _cache_key(text: str, provider: str, model: str, dimension: int) -> str:
    """Build the cache key for one embedded text.

    Args:
        text: The text as it will be sent to the provider.
        provider: Provider name.
        model: Model identifier.
        dimension: Vector width.

    Returns:
        A key in the ``embedding`` namespace. :func:`app.cache.make_key` hashes the parts,
        so the text itself is never written into a key or a filename.
    """
    return make_key(NAMESPACES.EMBEDDING, provider, model, dimension, text)


def _is_usable_vector(value: Any, dimension: int) -> bool:
    """Return whether a cached value is a vector of the expected width.

    A cache entry written by an older algorithm, a different width, or a hand edit must be
    treated as a miss rather than trusted.

    Args:
        value: Whatever the cache returned.
        dimension: The width the current embedder produces.

    Returns:
        ``True`` when *value* is a list of numbers of exactly that width.
    """
    return (
        isinstance(value, list)
        and len(value) == dimension
        and all(isinstance(component, (int, float)) for component in value)
    )


async def embed_texts(
    texts: Sequence[str],
    *,
    embedder: Embedder | None = None,
    use_cache: bool | None = None,
) -> list[list[float]]:
    """Embed *texts*, reusing everything already embedded.

    The path that matters for performance. Re-indexing a knowledge source re-presents
    thousands of chunks that have not changed since the last run, so this:

    1. blanks out empty inputs, which never reach a provider;
    2. collapses duplicate texts inside the batch to one upstream item;
    3. reads :mod:`app.cache` (namespace ``embedding``, keyed by provider, model, width
       and text) for the rest, concurrently;
    4. sends only the misses to the embedder;
    5. writes the fresh vectors back, concurrently;
    6. reassembles the result in the caller's original order.

    Args:
        texts: The texts to embed, in the order the result must come back in.
        embedder: The embedder to use; :func:`get_embedder` when omitted.
        use_cache: Overrides ``settings.cache_embeddings``. ``False`` bypasses the cache
            for both reads and writes.

    Returns:
        One vector per input, in input order. An empty or whitespace-only input yields a
        zero vector of the embedder's width rather than a provider round trip.

    Raises:
        EmbeddingError: On a provider failure, or when the embedder returns the wrong
            number of vectors.
        EmbeddingDimensionMismatch: When the provider's width is not
            ``settings.embedding_dim``.
    """
    if not texts:
        return []

    from app.config.settings import get_settings

    active = embedder if embedder is not None else get_embedder()
    provider, model, dimension = embedder_identity(active)
    caching = bool(get_settings().cache_embeddings) if use_cache is None else use_cache

    results: list[list[float] | None] = [None] * len(texts)

    # Group the positions of every distinct non-blank text: identical chunks (a licence
    # header, a repeated README section) are embedded once, not once per occurrence.
    # Blanks are left as ``None`` and filled with a zero vector at the end, once the width
    # is certain even for a custom embedder that declares none.
    positions: dict[str, list[int]] = {}
    blanks: list[int] = []
    for index, text in enumerate(texts):
        if not text or not text.strip():
            blanks.append(index)
            continue
        positions.setdefault(text, []).append(index)

    if not positions:
        return [[0.0] * max(dimension, 0) for _ in texts]

    distinct = list(positions)
    misses = distinct
    hits = 0

    if caching and dimension > 0:
        cache = get_cache()
        keys = [_cache_key(text, provider, model, dimension) for text in distinct]
        cached_values = await asyncio.gather(*(cache.get(key) for key in keys))
        misses = []
        for text, value in zip(distinct, cached_values, strict=True):
            if _is_usable_vector(value, dimension):
                vector = [float(component) for component in value]
                for index in positions[text]:
                    results[index] = list(vector)
                hits += 1
            else:
                misses.append(text)

    if misses:
        fresh = await active.embed(list(misses))
        if len(fresh) != len(misses):
            raise EmbeddingError(
                f"embedder returned {len(fresh)} vectors for {len(misses)} texts",
                provider=provider,
                model=model,
            )
        for text, vector in zip(misses, fresh, strict=True):
            for index in positions[text]:
                results[index] = list(vector)
        if caching and dimension > 0:
            cache = get_cache()
            await asyncio.gather(
                *(
                    cache.set(
                        _cache_key(text, provider, model, dimension),
                        list(vector),
                        ttl=EMBEDDING_CACHE_TTL_SECONDS,
                    )
                    for text, vector in zip(misses, fresh, strict=True)
                )
            )

    logger.debug(
        "embeddings.batch",
        provider=provider,
        model=model,
        texts=len(texts),
        distinct=len(distinct),
        cache_hits=hits,
        embedded=len(misses),
    )

    if blanks:
        width = dimension if dimension > 0 else _observed_width(results)
        zero_vector = [0.0] * width
        for index in blanks:
            results[index] = list(zero_vector)
    return [vector if vector is not None else [] for vector in results]


def _observed_width(vectors: Sequence[list[float] | None]) -> int:
    """Return the width of the first real vector in *vectors*.

    The fallback for an :class:`Embedder` that declares no ``dimension`` of its own: a
    blank input must still yield a zero vector the vector store will accept alongside the
    real ones, so the width is read off a sibling rather than guessed.

    Args:
        vectors: Partially-filled results, with ``None`` where a blank input sits.

    Returns:
        The first non-``None`` entry's width, or ``0`` when there is none.
    """
    for vector in vectors:
        if vector is not None:
            return len(vector)
    return 0


# ======================================================================================
# Resolution
# ======================================================================================

#: Memoised embedder instances, keyed by resolved provider name.
_embedders: dict[str, Embedder] = {}

#: Guards :data:`_embedders` and :data:`_warned_providers`.
_embedder_lock: Final[threading.RLock] = threading.RLock()

#: Providers already warned about, so the "falling back to hashing" message is logged
#: exactly once per process rather than on every batch.
_warned_providers: set[str] = set()

#: Constructors for each provider name.
_EMBEDDER_CLASSES: Final[dict[str, type[BaseEmbedder]]] = {
    OPENAI_PROVIDER: OpenAIEmbedder,
    LOCAL_PROVIDER: LocalEmbedder,
    HASHING_PROVIDER: HashingEmbedder,
}


def _warn_once(provider: str, event: str, **fields: Any) -> None:
    """Log *event* for *provider* at most once per process.

    Args:
        provider: The configured provider name.
        event: The structlog event name.
        **fields: Additional bound fields.
    """
    with _embedder_lock:
        if provider in _warned_providers:
            return
        _warned_providers.add(provider)
    logger.warning(event, provider=provider, fallback=HASHING_PROVIDER, **fields)


def resolve_embedding_provider(settings: Settings) -> str:
    """Return the embedding provider to actually use, honouring the offline fallback.

    ``settings.embedding_provider`` states the intent; this answers what is usable. A
    provider whose API key is missing, or whose SDK is not installed, resolves to
    :data:`HASHING_PROVIDER` with a single warning — ApplicantOS is required to run end to
    end with zero API keys and no optional packages.

    Args:
        settings: The settings to read.

    Returns:
        A key of :data:`_EMBEDDER_CLASSES`.
    """
    provider = str(settings.embedding_provider)
    if provider not in _EMBEDDER_CLASSES:
        _warn_once(provider, "embeddings.unknown_provider")
        return HASHING_PROVIDER

    key_field = PROVIDER_API_KEY_FIELDS.get(provider)
    if key_field is not None:
        api_key = getattr(settings, key_field, None)
        if not (isinstance(api_key, str) and api_key.strip()):
            _warn_once(
                provider,
                "embeddings.api_key_missing",
                setting=key_field.upper(),
                detail="embedding offline with the deterministic hashing embedder",
            )
            return HASHING_PROVIDER

    distribution = PROVIDER_REQUIRED_DISTRIBUTIONS.get(provider)
    if distribution is not None and not _is_installed(distribution):
        _warn_once(
            provider,
            "embeddings.sdk_missing",
            package=distribution,
            detail="embedding offline with the deterministic hashing embedder",
        )
        return HASHING_PROVIDER

    return provider


def _is_installed(distribution: str) -> bool:
    """Return whether *distribution* is importable, without importing it.

    Args:
        distribution: A top-level module name.

    Returns:
        ``True`` when the import system can locate it.
    """
    try:
        return find_spec(distribution) is not None
    except (ImportError, ValueError):  # pragma: no cover - broken sys.path entries
        return False


def build_embedder(settings: Settings) -> Embedder:
    """Construct the embedder described by *settings*, without memoising it.

    Args:
        settings: Configuration supplying ``embedding_provider``, ``embedding_model`` and
            ``embedding_dim``.

    Returns:
        A ready-to-use embedder. Construction never touches the network and never imports
        a provider SDK.
    """
    provider = resolve_embedding_provider(settings)
    embedder = _EMBEDDER_CLASSES[provider](settings)
    logger.debug(
        "embeddings.configured",
        provider=provider,
        model=embedder.model,
        dimension=embedder.dimension,
    )
    return embedder


def get_embedder() -> Embedder:
    """Return the process-wide embedder for ``settings.embedding_provider``.

    Memoised per resolved provider, so every caller shares one SDK client and one
    connection pool. A missing API key or a missing SDK degrades to
    :class:`HashingEmbedder` with one warning.

    Returns:
        The configured :class:`Embedder`.
    """
    from app.config.settings import get_settings

    settings = get_settings()
    provider = resolve_embedding_provider(settings)

    with _embedder_lock:
        existing = _embedders.get(provider)
        if existing is not None:
            return existing

    embedder = build_embedder(settings)

    with _embedder_lock:
        return _embedders.setdefault(provider, embedder)


def reset_embedder_cache() -> None:
    """Discard memoised embedders and the once-only warning state.

    For tests and for a settings reload: the next :func:`get_embedder` re-resolves the
    provider and rebuilds against current settings.
    """
    with _embedder_lock:
        _embedders.clear()
        _warned_providers.clear()
