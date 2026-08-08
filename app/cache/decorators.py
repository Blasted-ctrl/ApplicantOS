"""Declarative caching for async functions.

The contract's rule is "cache everything, reuse cached work whenever possible", and the
ergonomic way to honour it is to declare the intent at the definition site::

    @cached(NAMESPACES.EMBEDDING, ttl=None)
    async def embed(model: str, text: str) -> list[float]:
        ...

    await embed("text-embedding-3-small", "hello")   # computed, then stored
    await embed("text-embedding-3-small", "hello")   # served from the cache
    await embed.invalidate("text-embedding-3-small", "hello")
    embed.cache_key("text-embedding-3-small", "hello")   # 'embedding:9f2c…'

What the decorator guarantees:

* **Stable keys.** The key is derived from the *bound* arguments — defaults applied, so
  ``f(1)`` and ``f(1, flag=False)`` share an entry when ``flag`` defaults to ``False`` —
  plus the function's qualified name, so two functions in one namespace cannot collide.
* **Methods work.** A leading ``self``/``cls`` is excluded from the key, because the
  receiver is normally a stateless service object. When instance state *does* change the
  result, pass an explicit ``key`` callable. The wrapper is a descriptor, so
  ``await service.compute.invalidate(21)`` drops exactly the entry
  ``await service.compute(21)`` created — the receiver is re-supplied on the caller's
  behalf, which a plain function attribute could not do.
* **No stampede.** The wrapper goes through :meth:`~app.cache.base.Cache.get_or_set`, so N
  concurrent calls for the same uncached key produce one call to the wrapped function.
* **Opt-out per call.** ``condition`` is evaluated against the call's arguments; when it
  returns ``False`` the wrapped function is invoked directly and nothing is written. This
  is how ``settings.cache_llm_responses`` and the "only cache at ``temperature == 0``"
  rule from ``docs/CONTRACTS.md`` §10 are expressed.
* **Precise invalidation.** ``.invalidate(*args, **kwargs)`` removes exactly the entry
  those arguments would produce — golden rule #9, "invalidate precisely".

The cache instance is resolved lazily per call through :func:`app.cache.get_cache`, so the
decorator can be applied at import time, long before settings or a Redis connection exist,
and so tests can swap the backend after the decorated function is defined.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Awaitable, Callable
from typing import Any, Final, ParamSpec, Protocol, TypeVar, cast

import structlog

from app.cache.base import Cache
from app.cache.keys import make_key, normalize_namespace

__all__ = ["CachedFunction", "cached", "invalidate"]

logger = structlog.get_logger(__name__)

P = ParamSpec("P")
R = TypeVar("R")

#: Leading parameter names treated as a receiver and excluded from key derivation.
RECEIVER_PARAMETER_NAMES: Final[frozenset[str]] = frozenset({"self", "cls"})

#: Parameter kinds that can hold a receiver — a keyword-only ``self`` is not a receiver.
_POSITIONAL_KINDS: Final[tuple[Any, ...]] = (
    inspect.Parameter.POSITIONAL_ONLY,
    inspect.Parameter.POSITIONAL_OR_KEYWORD,
)


class CachedFunction(Protocol[P, R]):
    """The callable :func:`cached` returns: the original function plus cache controls."""

    __name__: str
    __qualname__: str
    __wrapped__: Callable[P, Awaitable[R]]

    async def __call__(self, *args: P.args, **kwargs: P.kwargs) -> R:
        """Return the cached result, computing it on a miss."""
        ...

    async def invalidate(self, *args: P.args, **kwargs: P.kwargs) -> None:
        """Drop the cache entry these arguments map to."""
        ...

    def cache_key(self, *args: P.args, **kwargs: P.kwargs) -> str:
        """Return the cache key these arguments map to, without touching the cache."""
        ...


def _default_cache() -> Cache:
    """Return the process-wide cache.

    Imported inside the function because :mod:`app.cache` imports this module in order to
    re-export :func:`cached`; a module-level import would be circular.

    Returns:
        The configured cache instance.
    """
    from app.cache import get_cache

    return get_cache()


def _build_key_builder(
    func: Callable[..., Any],
    namespace: str,
    key: Callable[..., Any] | None,
) -> Callable[..., str]:
    """Construct the function that maps a call's arguments to a cache key.

    Args:
        func: The function being decorated, used for its signature and qualified name.
        namespace: The cache namespace every derived key is prefixed with.
        key: Optional caller-supplied key function. It receives the same arguments as
            *func*; a returned tuple is spread across :func:`~app.cache.keys.make_key`'s
            parts, anything else becomes a single part. Its result is always hashed into
            the namespace, so a caller cannot accidentally produce an unnamespaced key.

    Returns:
        A callable accepting the wrapped function's arguments and returning a cache key.
    """
    if key is not None:

        def build_from_callable(*args: Any, **kwargs: Any) -> str:
            derived = key(*args, **kwargs)
            if isinstance(derived, tuple):
                return make_key(namespace, *derived)
            return make_key(namespace, derived)

        return build_from_callable

    signature = inspect.signature(func)
    parameters = list(signature.parameters.values())
    receiver_name: str | None = None
    if (
        parameters
        and parameters[0].name in RECEIVER_PARAMETER_NAMES
        and parameters[0].kind in _POSITIONAL_KINDS
    ):
        receiver_name = parameters[0].name
    identity = f"{func.__module__}.{func.__qualname__}"

    def build_from_signature(*args: Any, **kwargs: Any) -> str:
        try:
            bound = signature.bind_partial(*args, **kwargs)
        except TypeError:
            # The call does not match the signature; it is about to raise anyway, but the
            # key must still be derivable for `.cache_key()` and `.invalidate()`.
            return make_key(namespace, identity, list(args), kwargs)
        bound.apply_defaults()
        arguments = dict(bound.arguments)
        if receiver_name is not None:
            arguments.pop(receiver_name, None)
        return make_key(namespace, identity, arguments)

    return build_from_signature


class _CachedCallable:
    """An async function wrapped with read-through caching, plus its cache controls.

    A class rather than a closure for one reason: it implements ``__get__``, which is what
    makes the cache controls correct on methods. Attribute access through an instance
    (``service.compute.invalidate(21)``) yields a :class:`_BoundCachedCallable` that
    re-supplies the receiver, so the key the controls compute is the same key the call
    itself used. A plain function attribute cannot do this — ``service.compute.invalidate``
    resolves straight through to the underlying function with the receiver already lost,
    which would silently invalidate the wrong entry.

    :func:`functools.wraps` is applied to the instance, so ``__name__``, ``__qualname__``,
    ``__doc__`` and ``__wrapped__`` all match the decorated function and
    :func:`inspect.signature` reports the original signature.
    """

    def __init__(
        self,
        func: Callable[..., Awaitable[Any]],
        *,
        build_key: Callable[..., str],
        ttl: int | None,
        condition: Callable[..., bool] | None,
    ) -> None:
        """Bind one decorated function to its caching policy.

        Args:
            func: The async function being wrapped.
            build_key: Maps a call's arguments to a cache key.
            ttl: Lifetime for entries this function produces.
            condition: Predicate deciding, per call, whether to use the cache at all.
        """
        self._func = func
        self._build_key = build_key
        self._ttl = ttl
        self._condition = condition
        functools.wraps(func)(self)

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Return the cached result for these arguments, computing it on a miss."""
        if self._condition is not None and not self._condition(*args, **kwargs):
            return await self._func(*args, **kwargs)

        key = self._build_key(*args, **kwargs)

        async def factory() -> Any:
            return await self._func(*args, **kwargs)

        return await _default_cache().get_or_set(key, factory, ttl=self._ttl)

    def cache_key(self, *args: Any, **kwargs: Any) -> str:
        """Return the key these arguments map to, without touching the cache.

        Args:
            *args: The positional arguments of the call being described.
            **kwargs: The keyword arguments of the call being described.

        Returns:
            The cache key.
        """
        return self._build_key(*args, **kwargs)

    async def invalidate(self, *args: Any, **kwargs: Any) -> None:
        """Drop the entry these arguments map to.

        Args:
            *args: The positional arguments of the call being invalidated.
            **kwargs: The keyword arguments of the call being invalidated.
        """
        key = self._build_key(*args, **kwargs)
        await _default_cache().delete(key)
        logger.debug("cache.invalidated", function=self._func.__qualname__, key=key)

    def __get__(self, instance: Any, owner: type | None = None) -> Any:
        """Bind this wrapper to *instance* when accessed as a method.

        Args:
            instance: The receiver, or ``None`` for access through the class.
            owner: The owning class.

        Returns:
            Itself for class-level access, or a bound view that re-supplies the receiver.
        """
        if instance is None:
            return self
        return _BoundCachedCallable(self, instance)

    def __repr__(self) -> str:
        """Return a description naming the wrapped function."""
        return f"<cached {self._func.__module__}.{self._func.__qualname__}>"


class _BoundCachedCallable:
    """A :class:`_CachedCallable` bound to a receiver, mirroring :class:`types.MethodType`.

    Every entry point re-supplies the receiver, so ``obj.method(x)``,
    ``obj.method.cache_key(x)`` and ``obj.method.invalidate(x)`` all agree on one key.
    """

    def __init__(self, parent: _CachedCallable, instance: Any) -> None:
        """Bind *parent* to *instance*.

        Args:
            parent: The unbound cached callable declared on the class.
            instance: The receiver to prepend to every call.
        """
        self._parent = parent
        self._instance = instance
        functools.wraps(parent._func)(self)

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Invoke the cached method on the bound receiver."""
        return await self._parent(self._instance, *args, **kwargs)

    def cache_key(self, *args: Any, **kwargs: Any) -> str:
        """Return the key this method call maps to on the bound receiver."""
        return self._parent.cache_key(self._instance, *args, **kwargs)

    async def invalidate(self, *args: Any, **kwargs: Any) -> None:
        """Drop the entry this method call maps to on the bound receiver."""
        await self._parent.invalidate(self._instance, *args, **kwargs)

    def __repr__(self) -> str:
        """Return a description naming the wrapped method and its receiver."""
        return f"<bound {self._parent!r} of {self._instance!r}>"


def cached(
    namespace: str,
    ttl: int | None = None,
    key: Callable[..., Any] | None = None,
    condition: Callable[..., bool] | None = None,
) -> Callable[[Callable[P, Awaitable[R]]], CachedFunction[P, R]]:
    """Cache the results of an async function.

    Args:
        namespace: Cache namespace for every entry this function produces; prefer a
            :class:`~app.cache.keys.NAMESPACES` constant.
        ttl: Lifetime of each entry in seconds. ``None`` defers to the backend default
            (``settings.cache_default_ttl``); a non-positive value stores entries without
            expiry.
        key: Optional function receiving the same arguments as the decorated function and
            returning the identifying part(s) of the key. Use it when the default
            argument-derived key is wrong — for example when an argument is an unstable
            object such as a database session, or when instance state matters on a method.
        condition: Optional predicate receiving the same arguments as the decorated
            function. When it returns ``False`` the cache is bypassed entirely: the
            function is called and its result is not stored.

    Returns:
        A decorator producing a :class:`CachedFunction`.

    Raises:
        TypeError: If applied to something that is not a coroutine function. Synchronous
            functions are not supported — every cache backend is async.
        ValueError: If *namespace* normalises to nothing.
    """

    def decorate(func: Callable[P, Awaitable[R]]) -> CachedFunction[P, R]:
        if not inspect.iscoroutinefunction(func):
            raise TypeError(
                f"@cached only supports async functions; {func.__qualname__} is synchronous"
            )

        # Validate the namespace eagerly, so a typo fails at import time rather than on
        # the first call, deep inside a pipeline run.
        normalize_namespace(namespace)
        wrapper = _CachedCallable(
            func,
            build_key=_build_key_builder(func, namespace, key),
            ttl=ttl,
            condition=condition,
        )
        return cast("CachedFunction[P, R]", wrapper)

    return decorate


async def invalidate(namespace: str, *parts: Any) -> None:
    """Delete one cache entry by namespace and key parts.

    The counterpart to :func:`~app.cache.keys.make_key`: pass the same parts that produced
    the entry and it is removed. Use this when the entry was written by
    :meth:`~app.cache.base.Cache.set` rather than by a decorated function — for a decorated
    function, ``func.invalidate(...)`` is precise and does not require restating the key.

    Args:
        namespace: The namespace the entry lives in.
        *parts: The same parts that were passed to :func:`~app.cache.keys.make_key`.
    """
    cache_key = make_key(namespace, *parts)
    await _default_cache().delete(cache_key)
    logger.debug("cache.invalidated", namespace=namespace, key=cache_key)
